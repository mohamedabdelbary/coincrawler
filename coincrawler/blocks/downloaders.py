import threading
import time
from datetime import datetime
from collections import deque

from coincrawler.utils.eta import BlockCollectionETA
from coincrawler.blocks.jobclient import JobClient

def prettyPrintBlock(block):
	blockCopy = {k: v for k, v in block.iteritems()}
	if type(blockCopy['timestamp']) is int:
		blockCopy['timestamp'] = datetime.utcfromtimestamp(blockCopy['timestamp'])
	return str(blockCopy["height"]) + " " + ", ".join([key + ": " + str(value) for key, value in blockCopy.iteritems() if key != "height"])


class IDownloader(object):

	silent = False

	def loadBlocks(self, blocksList):
		return None

	def silence(self):
		self.silent = True


class SerialDownloader(IDownloader):

	def __init__(self, dataSource, etaMaxObservations=100, etaReportInterval=10, sleepBetweenRequests=0):
		self.dataSource = dataSource
		self.etaMaxObservations = etaMaxObservations
		self.etaReportInterval = etaReportInterval
		self.sleepBetweenRequests = sleepBetweenRequests

	def getBlockHeight(self):
		return self.dataSource.getBlockHeight()

	def loadBlocks(self, blocksList):
		fromHeight = blocksList[0]
		toHeight = blocksList[-1]
		eta = BlockCollectionETA(toHeight - fromHeight + 1, self.etaMaxObservations, self.etaReportInterval, silent=self.silent)
		for i in xrange(toHeight - fromHeight + 1):
			eta.workStarted()
			if self.sleepBetweenRequests > 0:
				time.sleep(self.sleepBetweenRequests)

			height = fromHeight + i
			block = None
			retries = 0
			while block is None:
				try:
					block = self.dataSource.getBlock(height)
				except Exception as e:
					if retries < 5:
						retries += 1
						print "failed to getblock, retrying"
						time.sleep(2 * self.sleepBetweenRequests)
					else:
						raise e

			if not self.silent:
				print "downloaded block %s" % prettyPrintBlock(block)
			eta.workFinished(1)

			yield block


class NetworkDownloader(IDownloader):

	def __init__(self, currency, host, port, etaMaxObservations=100, etaReportInterval=10, sleepBetweenRequests=0, amountPerRequest=10):
		self.currency = currency
		self.etaMaxObservations = etaMaxObservations
		self.etaReportInterval = etaReportInterval
		self.sleepBetweenRequests = sleepBetweenRequests
		self.amountPerRequest = amountPerRequest
		self.client = JobClient(host, port)

	def getBlockHeight(self):
		result, error = self.client.issueCommand("getNetworkBlockHeight", self.currency)
		if error is not None:
			result = 0
			print error
		return result

	def loadBlocks(self, blocksList):
		fromHeight = blocksList[0]
		toHeight = blocksList[-1]
		jobId, error = self.client.issueCommand("startJob", self.currency, fromHeight, toHeight)
		if error is not None:
			print error
			raise Exception()

		eta = BlockCollectionETA(toHeight - fromHeight + 1, self.etaMaxObservations, self.etaReportInterval, silent=self.silent)
		height = fromHeight
		skipWorkStarted = False
		while height <= toHeight:
			if not skipWorkStarted:
				eta.workStarted()
			result, error = self.client.issueCommand("getJobResult", jobId, height, height + self.amountPerRequest)
			if error is not None:
				print error
				raise Exception()

			receivedBlocks = 0
			for block in result:
				if block is not None:
					receivedBlocks += 1
					height += 1
					if not self.silent:
						print "downloaded block %s" % prettyPrintBlock(block)
					yield block
				else:
					break

			if receivedBlocks > 0:
				eta.workFinished(receivedBlocks)
				skipWorkStarted = False
			else:
				skipWorkStarted = True

			if self.sleepBetweenRequests > 0:
				time.sleep(self.sleepBetweenRequests)

		self.client.issueCommand("stopJob", jobId)


class MultisourceDownloader(IDownloader):

	def __init__(self, downloaders, countPerJob, etaMaxObservations=100, etaReportInterval=10):
		assert(len(downloaders) > 0)
		self.downloaders = downloaders
		self.countPerJob = countPerJob
		self.etaMaxObservations = etaMaxObservations
		self.etaReportInterval = etaReportInterval

	def getBlockHeight(self):
		return self.downloaders[0].getBlockHeight()

	def loadBlocks(self, blocksList):
		self.jobs = deque()
		n = 0
		while n < len(blocksList):
			batch = []
			while len(batch) < self.countPerJob and n < len(blocksList):
				batch.append(blocksList[n])
				n += 1
			self.jobs.append(batch)

		self.threads = []
		self.lock = threading.Lock()
		self.results = {}
		self.currentBlock = blocksList[0]
		for downloader in self.downloaders:
			downloader.silence()
			thread = threading.Thread(None, self.downloaderThread, "", (downloader,))
			thread.start()

		eta = BlockCollectionETA(len(blocksList), self.etaMaxObservations, self.etaReportInterval)
		eta.workStarted()
		while self.currentBlock != blocksList[-1] + 1:
			while self.currentBlock in self.results:
				print "downloaded block %s" % prettyPrintBlock(self.results[self.currentBlock])
				yield self.results[self.currentBlock]
				del self.results[self.currentBlock]
				self.currentBlock += 1
				if ((self.currentBlock - blocksList[0]) % self.countPerJob == 0):
					eta.workFinished(self.countPerJob)
					eta.workStarted()
			time.sleep(1)

		for thread in self.threads:
			thread.join()

	def downloaderThread(self, downloader):
		while True:
			batch = None
			self.lock.acquire()
			if len(self.jobs) > 0:
				batch = self.jobs.popleft()
			self.lock.release()

			if batch is not None:
				n = 0
				for block in downloader.loadBlocks(batch):
					assert(batch[n] == block['height'])
					self.results[batch[n]] = block
					n += 1
			else:
				break