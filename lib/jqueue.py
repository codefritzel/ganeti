#
#

# Copyright (C) 2006, 2007 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""Module implementing the job queue handling."""

import logging
import threading

from ganeti import constants
from ganeti import workerpool
from ganeti import errors
from ganeti import mcpu
from ganeti import utils


JOBQUEUE_THREADS = 5


class _QueuedOpCode(object):
  """Encasulates an opcode object.

  Access must be synchronized by using an external lock.

  """
  def __init__(self, op):
    self.input = op
    self.status = constants.OP_STATUS_QUEUED
    self.result = None


class _QueuedJob(object):
  """In-memory job representation.

  This is what we use to track the user-submitted jobs.

  """
  def __init__(self, ops, job_id):
    if not ops:
      # TODO
      raise Exception("No opcodes")

    self.id = job_id
    self._lock = threading.Lock()

    # _ops should not be modified again because we don't acquire the lock
    # to use it.
    self._ops = [_QueuedOpCode(op) for op in ops]

  def _GetStatusUnlocked(self):
    status = constants.JOB_STATUS_QUEUED

    all_success = True
    for op in self._ops:
      if op.status == constants.OP_STATUS_SUCCESS:
        continue

      all_success = False

      if op.status == constants.OP_STATUS_QUEUED:
        pass
      elif op.status == constants.OP_STATUS_ERROR:
        status = constants.JOB_STATUS_ERROR
      elif op.status == constants.OP_STATUS_RUNNING:
        status = constants.JOB_STATUS_RUNNING

    if all_success:
      status = constants.JOB_STATUS_SUCCESS

    return status

  def GetStatus(self):
    self._lock.acquire()
    try:
      return self._GetStatusUnlocked()
    finally:
      self._lock.release()

  def Run(self, proc):
    """Job executor.

    This functions processes a this job in the context of given processor
    instance.

    Args:
    - proc: Ganeti Processor to run the job with

    """
    try:
      count = len(self._ops)
      for idx, op in enumerate(self._ops):
        try:
          self._lock.acquire()
          try:
            logging.debug("Op %s/%s: Starting %s", idx + 1, count, op)
            op.status = constants.OP_STATUS_RUNNING
          finally:
            self._lock.release()

          result = proc.ExecOpCode(op.input)

          self._lock.acquire()
          try:
            logging.debug("Op %s/%s: Successfully finished %s",
                          idx + 1, count, op)
            op.status = constants.OP_STATUS_SUCCESS
            op.result = result
          finally:
            self._lock.release()
        except Exception, err:
          self._lock.acquire()
          try:
            logging.debug("Op %s/%s: Error in %s", idx + 1, count, op)
            op.status = constants.OP_STATUS_ERROR
            op.result = str(err)
          finally:
            self._lock.release()
          raise

    except errors.GenericError, err:
      logging.error("ganeti exception %s", exc_info=err)
    except Exception, err:
      logging.error("unhandled exception %s", exc_info=err)
    except:
      logging.error("unhandled unknown exception %s", exc_info=err)


class _JobQueueWorker(workerpool.BaseWorker):
  def RunTask(self, job):
    logging.debug("Worker %s processing job %s",
                  self.worker_id, job.id)
    # TODO: feedback function
    proc = mcpu.Processor(self.pool.context, feedback=lambda x: None)
    try:
      job.Run(proc)
    finally:
      logging.debug("Worker %s finished job %s, status = %s",
                    self.worker_id, job.id, job.GetStatus())


class _JobQueueWorkerPool(workerpool.WorkerPool):
  def __init__(self, context):
    super(_JobQueueWorkerPool, self).__init__(JOBQUEUE_THREADS,
                                              _JobQueueWorker)
    self.context = context


class JobQueue:
  """The job queue.

   """
  def __init__(self, context):
    self._lock = threading.Lock()
    self._last_job_id = 0
    self._jobs = {}
    self._wpool = _JobQueueWorkerPool(context)

  def _NewJobIdUnlocked(self):
    """Generates a new job identifier.

    Returns: A string representing the job identifier.

    """
    self._last_job_id += 1
    return str(self._last_job_id)

  def SubmitJob(self, ops):
    """Add a new job to the queue.

    This enters the job into our job queue and also puts it on the new
    queue, in order for it to be picked up by the queue processors.

    Args:
    - ops: Sequence of opcodes

    """
    # Get job identifier
    self._lock.acquire()
    try:
      job_id = self._NewJobIdUnlocked()
    finally:
      self._lock.release()

    job = _QueuedJob(ops, job_id)

    # Add it to our internal queue
    self._lock.acquire()
    try:
      self._jobs[job_id] = job
    finally:
      self._lock.release()

    # Add to worker pool
    self._wpool.AddTask(job)

    return job_id

  def ArchiveJob(self, job_id):
    raise NotImplementedError()

  def CancelJob(self, job_id):
    raise NotImplementedError()

  def _GetJobInfo(self, job, fields):
    row = []
    for fname in fields:
      if fname == "id":
        row.append(job.id)
      elif fname == "status":
        row.append(job.GetStatus())
      elif fname == "result":
        # TODO
        row.append(map(lambda op: op.result, job._ops))
      else:
        raise errors.OpExecError("Invalid job query field '%s'" % fname)
    return row

  def QueryJobs(self, job_ids, fields):
    """Returns a list of jobs in queue.

    Args:
    - job_ids: Sequence of job identifiers or None for all
    - fields: Names of fields to return

    """
    self._lock.acquire()
    try:
      if not job_ids:
        job_ids = self._jobs.keys()

      # TODO: define sort order?
      job_ids.sort()

      jobs = []

      for job_id in job_ids:
        job = self._jobs.get(job_id, None)
        if job is None:
          jobs.append(None)
        else:
          jobs.append(self._GetJobInfo(job, fields))

      return jobs
    finally:
      self._lock.release()

  def Shutdown(self):
    """Stops the job queue.

    """
    self._wpool.TerminateWorkers()
