#!/usr/bin/env python3


from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ..activities.summarization import summarization
    from ..common.retry_policies import DEFAULT_RETRY_POLICY
    from ..env import temporal_heartbeat_timeout, temporal_schedule_to_close_timeout


@workflow.defn
class SummarizationWorkflow:
    @workflow.run
    async def run(self, session_id: str) -> None:
        return await workflow.execute_activity(
            summarization,
            session_id,
            schedule_to_close_timeout=timedelta(
                seconds=temporal_schedule_to_close_timeout
            ),
            retry_policy=DEFAULT_RETRY_POLICY,
            heartbeat_timeout=timedelta(seconds=temporal_heartbeat_timeout),
        )
