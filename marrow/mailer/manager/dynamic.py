import atexit
import math
import sys
import threading
import weakref

from concurrent import futures
from functools import partial
from queue import Empty
from threading import Thread, Lock, current_thread

from .futures import worker
from .util import TransportPool


__all__ = ['DynamicManager']

log = __import__('logging').getLogger(__name__)


def thread_worker(executor, jobs, timeout, maximum):
	i = maximum + 1
	
	try:
		while i:
			i -= 1
			
			try:
				work = jobs.get(True, timeout)
				
				if work is None:
					runner = executor()
					
					if runner is None or runner._shutdown:
						if __debug__: log.debug("Worker instructed to shut down.")
						break
					
					# Can't think of a test case for this; best to be safe.
					del runner	# pragma: no cover
					continue  # pragma: no cover
				
			except Empty:  # pragma: no cover
				if __debug__: log.debug("Worker death from starvation.")
				break
			
			else:
				work.run()
		
		else:  # pragma: no cover
			if __debug__: log.debug("Worker death from exhaustion.")
	
	except:  # pragma: no cover
		log.critical("Unhandled exception in worker.", exc_info=True)
	
	runner = executor()
	if runner:
		runner._threads.discard(current_thread())


class WorkItem:
	__slots__ = ('future', 'fn', 'args', 'kwargs')
	
	def __init__(self, future, fn, args, kwargs):
		self.future = future
		self.fn = fn
		self.args = args
		self.kwargs = kwargs
	
	def run(self):
		if not self.future.set_running_or_notify_cancel():
			return
		
		try:
			result = self.fn(*self.args, **self.kwargs)
		
		except:
			e = sys.exc_info()[1]
			self.future.set_exception(e)
		
		else:
			self.future.set_result(result)


class ScalingPoolExecutor(futures.ThreadPoolExecutor):
	def __init__(self, workers, divisor, timeout, **kw):
		self.divisor = divisor
		self.timeout = timeout
		self._management_lock = threading.Lock()
		
		super().__init__(workers, **kw)  # Permit pass-through of thread_name_prefix, initializer, and initargs.
		
		atexit.register(self._atexit)
	
	def shutdown(self, wait=True):
		with self._shutdown_lock:
			self._shutdown = True
			
			for i in range(len(self._threads)):
				self._work_queue.put(None)
		
		if wait:
			for thread in list(self._threads):
				thread.join()
	
	def _atexit(self):	# pragma: no cover
		self.shutdown(True)
	
	def _spawn(self):
		t = Thread(target=thread_worker, args=(weakref.ref(self), self._work_queue, self.divisor, self.timeout))
		t.daemon = True
		t.start()
		
		with self._management_lock:
			self._threads.add(t)
	
	def _adjust_thread_count(self):
		pool = len(self._threads)
		
		if pool < self._optimum_workers:
			tospawn = int(self._optimum_workers - pool)
			if __debug__: log.debug("Spawning %d thread%s." % (tospawn, tospawn != 1 and "s" or ""))
			
			for i in range(tospawn):
				self._spawn()
	
	@property
	def _optimum_workers(self):
		return min(self._max_workers, math.ceil(self._work_queue.qsize() / float(self.divisor)))


class DynamicManager:
	__slots__ = ('workers', 'divisor', 'timeout', 'executor', 'transport')
	
	name = "Dynamic"
	Executor = ScalingPoolExecutor
	
	def __init__(self, config, transport):
		self.workers = int(config.get('workers', 10))  # Maximum number of threads to create.
		self.divisor = int(config.get('divisor', 10))  # Estimate the number of required threads.
		self.timeout = float(config.get('timeout', 60))  # Seconds before starvation.
		
		self.executor = None
		self.transport = TransportPool(transport)
		
		super().__init__()
	
	def startup(self):
		log.info("%s manager starting up.", self.name)
		
		if __debug__: log.debug("Initializing transport queue.")
		self.transport.startup()
		
		workers = self.workers
		if __debug__: log.debug("Starting thread pool with %d workers." % (workers, ))
		self.executor = self.Executor(workers, self.divisor, self.timeout)
		
		log.info("%s manager ready.", self.name)
	
	def deliver(self, message):
		# Return the Future object so the application can register callbacks.
		# We pass the message so the executor can do what it needs to to make
		# the message thread-local.
		return self.executor.submit(partial(worker, self.transport), message)
	
	def shutdown(self, wait=True):
		log.info("%s manager stopping.", self.name)
		
		if __debug__: log.debug("Stopping thread pool.")
		self.executor.shutdown(wait=wait)
		
		if __debug__: log.debug("Draining transport queue.")
		self.transport.shutdown()
		
		log.info("%s manager stopped.", self.name)
