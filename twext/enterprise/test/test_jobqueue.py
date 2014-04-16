##
# Copyright (c) 2012-2014 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
Tests for L{twext.enterprise.job.queue}.
"""

import datetime

from zope.interface.verify import verifyObject

from twisted.trial.unittest import TestCase, SkipTest
from twisted.test.proto_helpers import StringTransport, MemoryReactor
from twisted.internet.defer import (
    Deferred, inlineCallbacks, gatherResults, passthru, returnValue
)
from twisted.internet.task import Clock as _Clock
from twisted.protocols.amp import Command, AMP, Integer
from twisted.application.service import Service, MultiService

from twext.enterprise.dal.syntax import SchemaSyntax, Select
from twext.enterprise.dal.record import fromTable
from twext.enterprise.dal.test.test_parseschema import SchemaTestHelper
from twext.enterprise.fixtures import buildConnectionPool
from twext.enterprise.fixtures import SteppablePoolHelper
from twext.enterprise.jobqueue import (
    inTransaction, PeerConnectionPool, astimestamp,
    LocalPerformer, _IJobPerformer, WorkItem, WorkerConnectionPool,
    ConnectionFromPeerNode, LocalQueuer,
    _BaseQueuer, NonPerformingQueuer
)
import twext.enterprise.jobqueue

# TODO: There should be a store-building utility within twext.enterprise.
try:
    from txdav.common.datastore.test.util import buildStore
except ImportError:
    def buildStore(*args, **kwargs):
        raise SkipTest(
            "buildStore is not available, because it's in txdav; duh."
        )



class Clock(_Clock):
    """
    More careful L{IReactorTime} fake which mimics the exception behavior of
    the real reactor.
    """

    def callLater(self, _seconds, _f, *args, **kw):
        if _seconds < 0:
            raise ValueError("%s<0: " % (_seconds,))
        return super(Clock, self).callLater(_seconds, _f, *args, **kw)



class MemoryReactorWithClock(MemoryReactor, Clock):
    """
    Simulate a real reactor.
    """
    def __init__(self):
        MemoryReactor.__init__(self)
        Clock.__init__(self)



def transactionally(transactionCreator):
    """
    Perform the decorated function immediately in a transaction, replacing its
    name with a L{Deferred}.

    Use like so::

        @transactionally(connectionPool.connection)
        @inlineCallbacks
        def it(txn):
            yield txn.doSomething()
        it.addCallback(firedWhenDone)

    @param transactionCreator: A 0-arg callable that returns an
        L{IAsyncTransaction}.
    """
    def thunk(operation):
        return inTransaction(transactionCreator, operation)
    return thunk



class UtilityTests(TestCase):
    """
    Tests for supporting utilities.
    """

    def test_inTransactionSuccess(self):
        """
        L{inTransaction} invokes its C{transactionCreator} argument, and then
        returns a L{Deferred} which fires with the result of its C{operation}
        argument when it succeeds.
        """
        class faketxn(object):
            def __init__(self):
                self.commits = []
                self.aborts = []

            def commit(self):
                self.commits.append(Deferred())
                return self.commits[-1]

            def abort(self):
                self.aborts.append(Deferred())
                return self.aborts[-1]

        createdTxns = []

        def createTxn(label):
            createdTxns.append(faketxn())
            return createdTxns[-1]

        dfrs = []

        def operation(t):
            self.assertIdentical(t, createdTxns[-1])
            dfrs.append(Deferred())
            return dfrs[-1]

        d = inTransaction(createTxn, operation)
        x = []
        d.addCallback(x.append)
        self.assertEquals(x, [])
        self.assertEquals(len(dfrs), 1)
        dfrs[0].callback(35)

        # Commit in progress, so still no result...
        self.assertEquals(x, [])
        createdTxns[0].commits[0].callback(42)

        # Committed, everything's done.
        self.assertEquals(x, [35])



class SimpleSchemaHelper(SchemaTestHelper):
    def id(self):
        return "worker"



SQL = passthru

nodeSchema = SQL(
    """
    create table NODE_INFO (
      HOSTNAME varchar(255) not null,
      PID integer not null,
      PORT integer not null,
      TIME timestamp default current_timestamp not null,
      primary key (HOSTNAME, PORT)
    );
    """
)

jobSchema = SQL(
    """
    create table JOB (
      JOB_ID      integer primary key default 1,
      WORK_TYPE   varchar(255) not null,
      PRIORITY    integer default 0,
      WEIGHT      integer default 0,
      NOT_BEFORE  timestamp default null,
      NOT_AFTER   timestamp default null
    );
    """
)

schemaText = SQL(
    """
    create table DUMMY_WORK_ITEM (
      WORK_ID integer primary key,
      JOB_ID integer references JOB,
      A integer, B integer,
      DELETE_ON_LOAD integer default 0
    );
    create table DUMMY_WORK_DONE (
      WORK_ID integer primary key,
      JOB_ID integer references JOB,
      A_PLUS_B integer
    );
    """
)

try:
    schema = SchemaSyntax(SimpleSchemaHelper().schemaFromString(jobSchema + schemaText))

    dropSQL = [
        "drop table {name} cascade".format(name=table)
        for table in ("DUMMY_WORK_ITEM", "DUMMY_WORK_DONE")
    ] + ["delete from job"]
except SkipTest as e:
    DummyWorkDone = DummyWorkItem = object
    skip = e
else:
    DummyWorkDone = fromTable(schema.DUMMY_WORK_DONE)
    DummyWorkItem = fromTable(schema.DUMMY_WORK_ITEM)
    skip = False



class DummyWorkDone(WorkItem, DummyWorkDone):
    """
    Work result.
    """



class DummyWorkItem(WorkItem, DummyWorkItem):
    """
    Sample L{WorkItem} subclass that adds two integers together and stores them
    in another table.
    """

    def doWork(self):
        if self.a == -1:
            raise ValueError("Ooops")
        return DummyWorkDone.makeJob(
            self.transaction, jobID=self.jobID + 100, workID=self.workID + 100, aPlusB=self.a + self.b
        )


    @classmethod
    @inlineCallbacks
    def loadForJob(cls, txn, *a):
        """
        Load L{DummyWorkItem} as normal...  unless the loaded item has
        C{DELETE_ON_LOAD} set, in which case, do a deletion of this same row in
        a concurrent transaction, then commit it.
        """
        workItems = yield super(DummyWorkItem, cls).loadForJob(txn, *a)
        if workItems[0].deleteOnLoad:
            otherTransaction = txn.concurrently()
            otherSelf = yield super(DummyWorkItem, cls).loadForJob(txn, *a)
            yield otherSelf[0].delete()
            yield otherTransaction.commit()
        returnValue(workItems)



class AMPTests(TestCase):
    """
    Tests for L{AMP} faithfully relaying ids across the wire.
    """

    def test_sendTableWithName(self):
        """
        You can send a reference to a table through a L{SchemaAMP} via
        L{TableSyntaxByName}.
        """
        client = AMP()

        class SampleCommand(Command):
            arguments = [("id", Integer())]

        class Receiver(AMP):
            @SampleCommand.responder
            def gotIt(self, id):
                self.it = id
                return {}

        server = Receiver()
        clientT = StringTransport()
        serverT = StringTransport()
        client.makeConnection(clientT)
        server.makeConnection(serverT)
        client.callRemote(SampleCommand, id=123)
        server.dataReceived(clientT.io.getvalue())
        self.assertEqual(server.it, 123)



class WorkItemTests(TestCase):
    """
    A L{WorkItem} is an item of work that can be executed.
    """

    def test_forTableName(self):
        """
        L{WorkItem.forTable} returns L{WorkItem} subclasses mapped to the given
        table.
        """
        self.assertIdentical(
            WorkItem.forTableName(schema.DUMMY_WORK_ITEM.model.name), DummyWorkItem
        )


    @inlineCallbacks
    def test_enqueue(self):
        """
        L{PeerConnectionPool.enqueueWork} will insert a job and a work item.
        """
        dbpool = buildConnectionPool(self, nodeSchema + jobSchema + schemaText)
        fakeNow = datetime.datetime(2012, 12, 12, 12, 12, 12)
        sinceEpoch = astimestamp(fakeNow)
        clock = Clock()
        clock.advance(sinceEpoch)
        qpool = PeerConnectionPool(clock, dbpool.connection, 0)
        realChoosePerformer = qpool.choosePerformer
        performerChosen = []

        def catchPerformerChoice():
            result = realChoosePerformer()
            performerChosen.append(True)
            return result

        qpool.choosePerformer = catchPerformerChoice

        @transactionally(dbpool.connection)
        def check(txn):
            return qpool.enqueueWork(
                txn, DummyWorkItem, a=3, b=9,
                notBefore=datetime.datetime(2012, 12, 13, 12, 12, 0)
            )

        proposal = yield check
        yield proposal.whenProposed()

        # Make sure we have one JOB and one DUMMY_WORK_ITEM
        @transactionally(dbpool.connection)
        def checkJob(txn):
            return Select(
                From=schema.JOB
            ).on(txn)

        jobs = yield checkJob
        self.assertTrue(len(jobs) == 1)
        self.assertTrue(jobs[0][1] == "DUMMY_WORK_ITEM")

        @transactionally(dbpool.connection)
        def checkWork(txn):
            return Select(
                From=schema.DUMMY_WORK_ITEM
            ).on(txn)

        work = yield checkWork
        self.assertTrue(len(work) == 1)
        self.assertTrue(work[0][1] == jobs[0][0])



class WorkerConnectionPoolTests(TestCase):
    """
    A L{WorkerConnectionPool} is responsible for managing, in a node's
    controller (master) process, the collection of worker (slave) processes
    that are capable of executing queue work.
    """



class WorkProposalTests(TestCase):
    """
    Tests for L{WorkProposal}.
    """

    @inlineCallbacks
    def test_whenProposedSuccess(self):
        """
        The L{Deferred} returned by L{WorkProposal.whenProposed} fires when the
        SQL sent to the database has completed.
        """
        cph = SteppablePoolHelper(nodeSchema + jobSchema + schemaText)
        cph.setUp(test=self)
        lq = LocalQueuer(cph.createTransaction)
        enqTxn = cph.createTransaction()
        wp = yield lq.enqueueWork(enqTxn, DummyWorkItem, a=3, b=4)
        r = yield wp.whenProposed()
        self.assertEquals(r, wp)


    def test_whenProposedFailure(self):
        """
        The L{Deferred} returned by L{WorkProposal.whenProposed} fails with an
        errback when the SQL executed to create the WorkItem row fails.
        """
        cph = SteppablePoolHelper(nodeSchema + jobSchema + schemaText)
        cph.setUp(self)
        enqTxn = cph.createTransaction()
        lq = LocalQueuer(cph.createTransaction)
        self.failUnlessFailure(lq.enqueueWork(enqTxn, DummyWorkItem, a=3, b=4, bogus=3), TypeError)
        enqTxn.abort()
        self.flushLoggedErrors()



class PeerConnectionPoolUnitTests(TestCase):
    """
    L{PeerConnectionPool} has many internal components.
    """
    def setUp(self):
        """
        Create a L{PeerConnectionPool} that is just initialized enough.
        """
        self.pcp = PeerConnectionPool(None, None, 4321)


    def checkPerformer(self, cls):
        """
        Verify that the performer returned by
        L{PeerConnectionPool.choosePerformer}.
        """
        performer = self.pcp.choosePerformer()
        self.failUnlessIsInstance(performer, cls)
        verifyObject(_IJobPerformer, performer)


    def test_choosingPerformerWhenNoPeersAndNoWorkers(self):
        """
        If L{PeerConnectionPool.choosePerformer} is invoked when no workers
        have spawned and no peers have established connections (either incoming
        or outgoing), then it chooses an implementation of C{performJob} that
        simply executes the work locally.
        """
        self.checkPerformer(LocalPerformer)


    def test_choosingPerformerWithLocalCapacity(self):
        """
        If L{PeerConnectionPool.choosePerformer} is invoked when some workers
        have spawned, then it should choose the worker pool as the local
        performer.
        """
        # Give it some local capacity.
        wlf = self.pcp.workerListenerFactory()
        proto = wlf.buildProtocol(None)
        proto.makeConnection(StringTransport())
        # Sanity check.
        self.assertEqual(len(self.pcp.workerPool.workers), 1)
        self.assertEqual(self.pcp.workerPool.hasAvailableCapacity(), True)
        # Now it has some capacity.
        self.checkPerformer(WorkerConnectionPool)


    def test_choosingPerformerFromNetwork(self):
        """
        If L{PeerConnectionPool.choosePerformer} is invoked when no workers
        have spawned but some peers have connected, then it should choose a
        connection from the network to perform it.
        """
        peer = PeerConnectionPool(None, None, 4322)
        local = self.pcp.peerFactory().buildProtocol(None)
        remote = peer.peerFactory().buildProtocol(None)
        connection = Connection(local, remote)
        connection.start()
        self.checkPerformer(ConnectionFromPeerNode)


    def test_performingWorkOnNetwork(self):
        """
        The L{performJob} command will get relayed to the remote peer
        controller.
        """
        peer = PeerConnectionPool(None, None, 4322)
        local = self.pcp.peerFactory().buildProtocol(None)
        remote = peer.peerFactory().buildProtocol(None)
        connection = Connection(local, remote)
        connection.start()
        d = Deferred()

        class DummyPerformer(object):
            def performJob(self, jobID):
                self.jobID = jobID
                return d

        # Doing real database I/O in this test would be tedious so fake the
        # first method in the call stack which actually talks to the DB.
        dummy = DummyPerformer()

        def chooseDummy(onlyLocally=False):
            return dummy

        peer.choosePerformer = chooseDummy
        performed = local.performJob(7384)
        performResult = []
        performed.addCallback(performResult.append)

        # Sanity check.
        self.assertEquals(performResult, [])
        connection.flush()
        self.assertEquals(dummy.jobID, 7384)
        self.assertEquals(performResult, [])
        d.callback(128374)
        connection.flush()
        self.assertEquals(performResult, [None])


    def test_choosePerformerSorted(self):
        """
        If L{PeerConnectionPool.choosePerformer} is invoked make it
        return the peer with the least load.
        """
        peer = PeerConnectionPool(None, None, 4322)

        class DummyPeer(object):
            def __init__(self, name, load):
                self.name = name
                self.load = load

            def currentLoadEstimate(self):
                return self.load

        apeer = DummyPeer("A", 1)
        bpeer = DummyPeer("B", 0)
        cpeer = DummyPeer("C", 2)
        peer.addPeerConnection(apeer)
        peer.addPeerConnection(bpeer)
        peer.addPeerConnection(cpeer)

        performer = peer.choosePerformer(onlyLocally=False)
        self.assertEqual(performer, bpeer)

        bpeer.load = 2
        performer = peer.choosePerformer(onlyLocally=False)
        self.assertEqual(performer, apeer)


    @inlineCallbacks
    def test_notBeforeWhenCheckingForLostWork(self):
        """
        L{PeerConnectionPool._periodicLostWorkCheck} should execute any
        outstanding work items, but only those that are expired.
        """
        dbpool = buildConnectionPool(self, nodeSchema + jobSchema + schemaText)
        # An arbitrary point in time.
        fakeNow = datetime.datetime(2012, 12, 12, 12, 12, 12)
        # *why* does datetime still not have .astimestamp()
        sinceEpoch = astimestamp(fakeNow)
        clock = Clock()
        clock.advance(sinceEpoch)
        qpool = PeerConnectionPool(clock, dbpool.connection, 0)

        # Let's create a couple of work items directly, not via the enqueue
        # method, so that they exist but nobody will try to immediately execute
        # them.

        @transactionally(dbpool.connection)
        @inlineCallbacks
        def setup(txn):
            # First, one that's right now.
            yield DummyWorkItem.makeJob(txn, a=1, b=2, notBefore=fakeNow)

            # Next, create one that's actually far enough into the past to run.
            yield DummyWorkItem.makeJob(
                txn, a=3, b=4, notBefore=(
                    # Schedule it in the past so that it should have already
                    # run.
                    fakeNow - datetime.timedelta(
                        seconds=qpool.queueProcessTimeout + 20
                    )
                )
            )

            # Finally, one that's actually scheduled for the future.
            yield DummyWorkItem.makeJob(
                txn, a=10, b=20, notBefore=fakeNow + datetime.timedelta(1000)
            )
        yield setup
        yield qpool._periodicLostWorkCheck()

        @transactionally(dbpool.connection)
        def check(txn):
            return DummyWorkDone.all(txn)

        every = yield check
        self.assertEquals([x.aPlusB for x in every], [7])


    @inlineCallbacks
    def test_notBeforeWhenEnqueueing(self):
        """
        L{PeerConnectionPool.enqueueWork} enqueues some work immediately, but
        only executes it when enough time has elapsed to allow the C{notBefore}
        attribute of the given work item to have passed.
        """
        dbpool = buildConnectionPool(self, nodeSchema + jobSchema + schemaText)
        fakeNow = datetime.datetime(2012, 12, 12, 12, 12, 12)
        sinceEpoch = astimestamp(fakeNow)
        clock = Clock()
        clock.advance(sinceEpoch)
        qpool = PeerConnectionPool(clock, dbpool.connection, 0)
        realChoosePerformer = qpool.choosePerformer
        performerChosen = []

        def catchPerformerChoice():
            result = realChoosePerformer()
            performerChosen.append(True)
            return result

        qpool.choosePerformer = catchPerformerChoice

        @transactionally(dbpool.connection)
        def check(txn):
            return qpool.enqueueWork(
                txn, DummyWorkItem, a=3, b=9,
                notBefore=datetime.datetime(2012, 12, 12, 12, 12, 20)
            )

        proposal = yield check
        yield proposal.whenProposed()

        # This is going to schedule the work to happen with some asynchronous
        # I/O in the middle; this is a problem because how do we know when it's
        # time to check to see if the work has started?  We need to intercept
        # the thing that kicks off the work; we can then wait for the work
        # itself.

        self.assertEquals(performerChosen, [])

        # Advance to exactly the appointed second.
        clock.advance(20 - 12)
        self.assertEquals(performerChosen, [True])

        # FIXME: if this fails, it will hang, but that's better than no
        # notification that it is broken at all.

        result = yield proposal.whenExecuted()
        self.assertIdentical(result, proposal)


    @inlineCallbacks
    def test_notBeforeBefore(self):
        """
        L{PeerConnectionPool.enqueueWork} will execute its work immediately if
        the C{notBefore} attribute of the work item in question is in the past.
        """
        dbpool = buildConnectionPool(self, nodeSchema + jobSchema + schemaText)
        fakeNow = datetime.datetime(2012, 12, 12, 12, 12, 12)
        sinceEpoch = astimestamp(fakeNow)
        clock = Clock()
        clock.advance(sinceEpoch)
        qpool = PeerConnectionPool(clock, dbpool.connection, 0)
        realChoosePerformer = qpool.choosePerformer
        performerChosen = []

        def catchPerformerChoice():
            result = realChoosePerformer()
            performerChosen.append(True)
            return result

        qpool.choosePerformer = catchPerformerChoice

        @transactionally(dbpool.connection)
        def check(txn):
            return qpool.enqueueWork(
                txn, DummyWorkItem, a=3, b=9,
                notBefore=datetime.datetime(2012, 12, 12, 12, 12, 0)
            )

        proposal = yield check
        yield proposal.whenProposed()

        clock.advance(1000)
        # Advance far beyond the given timestamp.
        self.assertEquals(performerChosen, [True])

        result = yield proposal.whenExecuted()
        self.assertIdentical(result, proposal)


    def test_workerConnectionPoolPerformJob(self):
        """
        L{WorkerConnectionPool.performJob} performs work by selecting a
        L{ConnectionFromWorker} and sending it a L{PerformJOB} command.
        """
        clock = Clock()
        peerPool = PeerConnectionPool(clock, None, 4322)
        factory = peerPool.workerListenerFactory()

        def peer():
            p = factory.buildProtocol(None)
            t = StringTransport()
            p.makeConnection(t)
            return p, t

        worker1, _ignore_trans1 = peer()
        worker2, _ignore_trans2 = peer()

        # Ask the worker to do something.
        worker1.performJob(1)
        self.assertEquals(worker1.currentLoad, 1)
        self.assertEquals(worker2.currentLoad, 0)

        # Now ask the pool to do something
        peerPool.workerPool.performJob(2)
        self.assertEquals(worker1.currentLoad, 1)
        self.assertEquals(worker2.currentLoad, 1)


    def test_poolStartServiceChecksForWork(self):
        """
        L{PeerConnectionPool.startService} kicks off the idle work-check loop.
        """
        reactor = MemoryReactorWithClock()
        cph = SteppablePoolHelper(nodeSchema + jobSchema + schemaText)
        then = datetime.datetime(2012, 12, 12, 12, 12, 0)
        reactor.advance(astimestamp(then))
        cph.setUp(self)
        pcp = PeerConnectionPool(reactor, cph.pool.connection, 4321)
        now = then + datetime.timedelta(seconds=pcp.queueProcessTimeout * 2)

        @transactionally(cph.pool.connection)
        def createOldWork(txn):
            one = DummyWorkItem.makeJob(txn, jobID=100, workID=1, a=3, b=4, notBefore=then)
            two = DummyWorkItem.makeJob(txn, jobID=101, workID=2, a=7, b=9, notBefore=now)
            return gatherResults([one, two])

        pcp.startService()
        cph.flushHolders()
        reactor.advance(pcp.queueProcessTimeout * 2)
        self.assertEquals(
            cph.rows("select * from DUMMY_WORK_DONE"),
            [(101, 200, 7)]
        )
        cph.rows("delete from DUMMY_WORK_DONE")
        reactor.advance(pcp.queueProcessTimeout * 2)
        self.assertEquals(
            cph.rows("select * from DUMMY_WORK_DONE"),
            [(102, 201, 16)]
        )


    @inlineCallbacks
    def test_exceptionWhenCheckingForLostWork(self):
        """
        L{PeerConnectionPool._periodicLostWorkCheck} should execute any
        outstanding work items, and keep going if some raise an exception.
        """
        dbpool = buildConnectionPool(self, nodeSchema + jobSchema + schemaText)
        # An arbitrary point in time.
        fakeNow = datetime.datetime(2012, 12, 12, 12, 12, 12)
        # *why* does datetime still not have .astimestamp()
        sinceEpoch = astimestamp(fakeNow)
        clock = Clock()
        clock.advance(sinceEpoch)
        qpool = PeerConnectionPool(clock, dbpool.connection, 0)

        # Let's create a couple of work items directly, not via the enqueue
        # method, so that they exist but nobody will try to immediately execute
        # them.

        @transactionally(dbpool.connection)
        @inlineCallbacks
        def setup(txn):
            # First, one that's right now.
            yield DummyWorkItem.makeJob(
                txn, a=1, b=0, notBefore=fakeNow - datetime.timedelta(20 * 60)
            )

            # Next, create one that's actually far enough into the past to run.
            yield DummyWorkItem.makeJob(
                txn, a=-1, b=1, notBefore=fakeNow - datetime.timedelta(20 * 60)
            )

            # Finally, one that's actually scheduled for the future.
            yield DummyWorkItem.makeJob(
                txn, a=2, b=0, notBefore=fakeNow - datetime.timedelta(20 * 60)
            )
        yield setup
        yield qpool._periodicLostWorkCheck()

        @transactionally(dbpool.connection)
        def check(txn):
            return DummyWorkDone.all(txn)

        every = yield check
        self.assertEquals([x.aPlusB for x in every], [1, 2])



class HalfConnection(object):
    def __init__(self, protocol):
        self.protocol = protocol
        self.transport = StringTransport()


    def start(self):
        """
        Hook up the protocol and the transport.
        """
        self.protocol.makeConnection(self.transport)


    def extract(self):
        """
        Extract the data currently present in this protocol's output buffer.
        """
        io = self.transport.io
        value = io.getvalue()
        io.seek(0)
        io.truncate()
        return value


    def deliver(self, data):
        """
        Deliver the given data to this L{HalfConnection}'s protocol's
        C{dataReceived} method.

        @return: a boolean indicating whether any data was delivered.
        @rtype: L{bool}
        """
        if data:
            self.protocol.dataReceived(data)
            return True
        return False



class Connection(object):

    def __init__(self, local, remote):
        """
        Connect two protocol instances to each other via string transports.
        """
        self.receiver = HalfConnection(local)
        self.sender = HalfConnection(remote)


    def start(self):
        """
        Start up the connection.
        """
        self.sender.start()
        self.receiver.start()


    def pump(self):
        """
        Relay data in one direction between the two connections.
        """
        result = self.receiver.deliver(self.sender.extract())
        self.receiver, self.sender = self.sender, self.receiver
        return result


    def flush(self, turns=10):
        """
        Keep relaying data until there's no more.
        """
        for _ignore_x in range(turns):
            if not (self.pump() or self.pump()):
                return



class PeerConnectionPoolIntegrationTests(TestCase):
    """
    L{PeerConnectionPool} is the service responsible for coordinating
    eventually-consistent task queuing within a cluster.
    """

    @inlineCallbacks
    def setUp(self):
        """
        L{PeerConnectionPool} requires access to a database and the reactor.
        """
        self.store = yield buildStore(self, None)

        def doit(txn):
            return txn.execSQL(schemaText)

        yield inTransaction(
            self.store.newTransaction,
            doit,
            label="bonus schema"
        )

        def indirectedTransactionFactory(*a, **b):
            """
            Allow tests to replace "self.store.newTransaction" to provide
            fixtures with extra methods on a test-by-test basis.
            """
            return self.store.newTransaction(*a, **b)

        def deschema():
            @inlineCallbacks
            def deletestuff(txn):
                for stmt in dropSQL:
                    yield txn.execSQL(stmt)
            return inTransaction(
                lambda *a, **b: self.store.newTransaction(*a, **b), deletestuff
            )
        self.addCleanup(deschema)

        from twisted.internet import reactor
        self.node1 = PeerConnectionPool(
            reactor, indirectedTransactionFactory, 0)
        self.node2 = PeerConnectionPool(
            reactor, indirectedTransactionFactory, 0)

        class FireMeService(Service, object):
            def __init__(self, d):
                super(FireMeService, self).__init__()
                self.d = d

            def startService(self):
                self.d.callback(None)

        d1 = Deferred()
        d2 = Deferred()
        FireMeService(d1).setServiceParent(self.node1)
        FireMeService(d2).setServiceParent(self.node2)
        ms = MultiService()
        self.node1.setServiceParent(ms)
        self.node2.setServiceParent(ms)
        ms.startService()
        self.addCleanup(ms.stopService)
        yield gatherResults([d1, d2])
        self.store.queuer = self.node1


    def test_currentNodeInfo(self):
        """
        There will be two C{NODE_INFO} rows in the database, retrievable as two
        L{NodeInfo} objects, once both nodes have started up.
        """
        @inlineCallbacks
        def check(txn):
            self.assertEquals(len((yield self.node1.activeNodes(txn))), 2)
            self.assertEquals(len((yield self.node2.activeNodes(txn))), 2)
        return inTransaction(self.store.newTransaction, check)


    @inlineCallbacks
    def test_enqueueHappyPath(self):
        """
        When a L{WorkItem} is scheduled for execution via
        L{PeerConnectionPool.enqueueWork} its C{doWork} method will be invoked
        by the time the L{Deferred} returned from the resulting
        L{WorkProposal}'s C{whenExecuted} method has fired.
        """
        # TODO: this exact test should run against LocalQueuer as well.
        def operation(txn):
            # TODO: how does "enqueue" get associated with the transaction?
            # This is not the fact with a raw t.w.enterprise transaction.
            # Should probably do something with components.
            return txn.enqueue(DummyWorkItem, a=3, b=4, jobID=100, workID=1,
                               notBefore=datetime.datetime.utcnow())
        result = yield inTransaction(self.store.newTransaction, operation)
        # Wait for it to be executed.  Hopefully this does not time out :-\.
        yield result.whenExecuted()

        def op2(txn):
            return Select(
                [
                    schema.DUMMY_WORK_DONE.WORK_ID,
                    schema.DUMMY_WORK_DONE.JOB_ID,
                    schema.DUMMY_WORK_DONE.A_PLUS_B,
                ],
                From=schema.DUMMY_WORK_DONE
            ).on(txn)

        rows = yield inTransaction(self.store.newTransaction, op2)
        self.assertEquals(rows, [[101, 200, 7]])


    @inlineCallbacks
    def test_noWorkDoneWhenConcurrentlyDeleted(self):
        """
        When a L{WorkItem} is concurrently deleted by another transaction, it
        should I{not} perform its work.
        """
        # Provide access to a method called "concurrently" everything using
        original = self.store.newTransaction

        def decorate(*a, **k):
            result = original(*a, **k)
            result.concurrently = self.store.newTransaction
            return result

        self.store.newTransaction = decorate

        def operation(txn):
            return txn.enqueue(
                DummyWorkItem, a=30, b=40, workID=5678,
                deleteOnLoad=1,
                notBefore=datetime.datetime.utcnow()
            )

        proposal = yield inTransaction(self.store.newTransaction, operation)
        yield proposal.whenExecuted()

        # Sanity check on the concurrent deletion.
        def op2(txn):
            return Select(
                [schema.DUMMY_WORK_ITEM.WORK_ID],
                From=schema.DUMMY_WORK_ITEM
            ).on(txn)

        rows = yield inTransaction(self.store.newTransaction, op2)
        self.assertEquals(rows, [])

        def op3(txn):
            return Select(
                [
                    schema.DUMMY_WORK_DONE.WORK_ID,
                    schema.DUMMY_WORK_DONE.A_PLUS_B,
                ],
                From=schema.DUMMY_WORK_DONE
            ).on(txn)

        rows = yield inTransaction(self.store.newTransaction, op3)
        self.assertEquals(rows, [])



class DummyProposal(object):

    def __init__(self, *ignored):
        pass


    def _start(self):
        pass



class BaseQueuerTests(TestCase):

    def setUp(self):
        self.proposal = None
        self.patch(twext.enterprise.jobqueue, "WorkProposal", DummyProposal)


    def _proposalCallback(self, proposal):
        self.proposal = proposal


    @inlineCallbacks
    def test_proposalCallbacks(self):
        queuer = _BaseQueuer()
        queuer.callWithNewProposals(self._proposalCallback)
        self.assertEqual(self.proposal, None)
        yield queuer.enqueueWork(None, None)
        self.assertNotEqual(self.proposal, None)



class NonPerformingQueuerTests(TestCase):

    @inlineCallbacks
    def test_choosePerformer(self):
        queuer = NonPerformingQueuer()
        performer = queuer.choosePerformer()
        result = (yield performer.performJob(None))
        self.assertEquals(result, None)
