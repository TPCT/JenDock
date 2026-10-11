import React from "react";
import { Box, Button, Chip, Stack, Typography } from "@mui/material";
import Hint from "./Hint";

const timeLabel = (value) =>
  typeof value === "string" && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString()
    : "Not recorded";
const outcomeLabel = (value) =>
  typeof value === "string"
    ? value.replaceAll("_", " ").replaceAll("-", " ")
    : "Not recorded";
const rowStyle = {
  p: 2,
  border: 1,
  borderColor: "divider",
  borderRadius: 1,
  minWidth: 0,
};

export function QueueObservation({ snapshot }) {
  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {snapshot.connection_id} · {snapshot.driver}
      </Typography>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", sm: "repeat(2,minmax(0,1fr))" },
          gap: 2,
        }}
      >
        {snapshot.queues.map((queue) => (
          <Stack key={queue.name} spacing={1} sx={rowStyle}>
            <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
              {queue.name}
            </Typography>
            <Stack direction="row" useFlexGap flexWrap="wrap" gap={1}>
              <Chip size="small" label={`Ready ${queue.ready}`} />
              <Chip size="small" label={`Delayed ${queue.delayed}`} />
              <Chip size="small" label={`Reserved ${queue.reserved}`} />
            </Stack>
          </Stack>
        ))}
      </Box>
      {!snapshot.queues.length && (
        <Typography color="text.secondary">No registered queues.</Typography>
      )}
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="subtitle1" fontWeight={600}>
          Workers
        </Typography>
        <Hint label="About worker observations">
          A heartbeat shows that instrumentation was observed. Completion and
          failure require recorded job events.
        </Hint>
      </Stack>
      {!snapshot.workers.length ? (
        <Typography color="text.secondary">
          No worker heartbeats recorded.
        </Typography>
      ) : (
        snapshot.workers.map((worker) => (
          <Stack key={worker.instance_id} spacing={0.5} sx={rowStyle}>
            <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
              {worker.instance_id}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Observed {timeLabel(worker.observed_at)}
            </Typography>
            <Typography variant="body2">
              Last outcome: {outcomeLabel(worker.last_outcome)}
            </Typography>
          </Stack>
        ))
      )}
    </Stack>
  );
}
export function FailureRecords({ items, onRetry, retryAllowed, disabled }) {
  if (!items.length)
    return (
      <Typography color="text.secondary">
        No failed jobs in this page.
      </Typography>
    );
  return (
    <Stack spacing={1}>
      {items.map((item) => (
        <Stack
          key={item.failed_job_id}
          direction={{ xs: "column", sm: "row" }}
          spacing={2}
          justifyContent="space-between"
          alignItems={{ sm: "center" }}
          sx={rowStyle}
        >
          <Box sx={{ minWidth: 0 }}>
            <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
              {item.failed_job_id}
            </Typography>
            <Typography variant="body2">
              {item.queue} · {timeLabel(item.failed_at)}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {outcomeLabel(item.classification)}
            </Typography>
          </Box>
          {retryAllowed && item.retry_supported && (
            <Button
              variant="outlined"
              disabled={disabled}
              onClick={() => onRetry(item)}
              sx={{ alignSelf: { xs: "flex-start", sm: "center" } }}
            >
              Retry job
            </Button>
          )}
        </Stack>
      ))}
    </Stack>
  );
}
export function ScheduleRecords({ items, history, registryRevision }) {
  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="body2" color="text.secondary">
          Registry {registryRevision}
        </Typography>
        <Hint label="About scheduled times">
          Next occurrence is supplied by the scheduler. Filters, locks,
          maintenance mode and timezone changes can prevent execution.
        </Hint>
      </Stack>
      {!items.length ? (
        <Typography color="text.secondary">No registered schedules.</Typography>
      ) : (
        items.map((item) => (
          <Stack key={item.schedule_id} spacing={1} sx={rowStyle}>
            <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
              {item.name}
            </Typography>
            <Typography
              component="code"
              variant="body2"
              sx={{ overflowWrap: "anywhere", minWidth: 0 }}
            >
              {item.expression} · {item.timezone}
            </Typography>
            <Typography variant="body2">
              Last outcome: {outcomeLabel(item.last_outcome)}
            </Typography>
            <Typography variant="body2">
              Next occurrence: {timeLabel(item.next_due_at)}
            </Typography>
            <Stack direction="row" useFlexGap flexWrap="wrap" gap={1}>
              <Chip
                size="small"
                label={
                  item.without_overlapping
                    ? "Overlap prevented"
                    : "Overlap allowed"
                }
              />
              {item.background_supported === false && (
                <Chip
                  size="small"
                  variant="outlined"
                  label="Background tracking unsupported"
                />
              )}
            </Stack>
          </Stack>
        ))
      )}
      <Typography variant="subtitle1" fontWeight={600}>
        Execution history
      </Typography>
      {!history.length ? (
        <Typography color="text.secondary">
          No execution events recorded.
        </Typography>
      ) : (
        history.map((event) => (
          <Stack key={event.event_id} spacing={0.5} sx={rowStyle}>
            <Typography sx={{ overflowWrap: "anywhere" }}>
              {items.find((item) => item.schedule_id === event.schedule_id)
                ?.name || event.schedule_id}
            </Typography>
            <Typography variant="body2">
              {outcomeLabel(event.outcome)} · {timeLabel(event.observed_at)}
            </Typography>
          </Stack>
        ))
      )}
    </Stack>
  );
}
