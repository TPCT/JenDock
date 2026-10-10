import React, { useEffect, useId, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Paper,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
  useMediaQuery,
  useTheme,
} from "@mui/material";

const WINDOWS = { "15m": 900, "1h": 3600, "24h": 86400 };
const finite = (value) => typeof value === "number" && Number.isFinite(value);
export const percentage = (used, total) =>
  finite(used) && finite(total) && total > 0 ? (used / total) * 100 : null;
export function formatBytes(value) {
  if (!finite(value)) return "Unavailable";
  const index = Math.min(
    4,
    Math.max(0, Math.floor(Math.log2(Math.max(1, value)) / 10)),
  );
  return `${(value / 1024 ** index).toLocaleString("en-GB", { maximumFractionDigits: index ? 1 : 0 })} ${["B", "KiB", "MiB", "GiB", "TiB"][index]}`;
}
const formatPercent = (value) =>
  finite(value) ? `${value.toFixed(1)}%` : "Unavailable";
const rate = (value) =>
  finite(value) ? `${formatBytes(value)}/s` : "Unavailable";
const clock = (time, date = false) =>
  new Date(time * 1000).toLocaleString(
    "en-GB",
    date
      ? {
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }
      : { hour: "2-digit", minute: "2-digit", second: "2-digit" },
  );

export function normalizeHistory(rows) {
  const unique = new Map();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (row && finite(row.observed)) unique.set(row.observed, row);
  }
  return [...unique.values()].sort((a, b) => a.observed - b.observed);
}
// Missing values and absent observations both break a line. Never interpolate them.
export function splitSeries(rows, value, maxGap) {
  const segments = [];
  let segment = [];
  let previous = null;
  for (const row of rows) {
    const reading = value(row);
    if (
      !finite(reading) ||
      (previous !== null && row.observed - previous > maxGap)
    ) {
      if (segment.length) segments.push(segment);
      segment = [];
    }
    if (finite(reading)) segment.push({ time: row.observed, value: reading });
    previous = row.observed;
  }
  if (segment.length) segments.push(segment);
  return segments;
}

const DEFINITIONS = [
  {
    key: "cpu",
    title: "CPU",
    color: "primary",
    value: (m) => (finite(m.cpu) ? formatPercent(m.cpu) : "Measuring…"),
    note: (m) =>
      [
        finite(m.cpu_count) ? `${m.cpu_count} CPUs` : null,
        finite(m.load_1m) ? `1m load ${m.load_1m.toFixed(2)}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    series: [{ label: "CPU", value: (r) => r.cpu }],
    format: formatPercent,
    maximum: 100,
  },
  {
    key: "memory",
    title: "Memory",
    color: "success",
    value: (m) => formatBytes(m.memory_used),
    note: (m) =>
      `${formatBytes(m.memory_total)} total · ${formatPercent(percentage(m.memory_used, m.memory_total))}`,
    series: [
      {
        label: "Memory used",
        value: (r) => percentage(r.memory_used, r.memory_total),
      },
    ],
    format: formatPercent,
    maximum: 100,
  },
  {
    key: "network",
    title: "Network",
    color: "primary",
    value: (m) =>
      finite(m.network_rx) && finite(m.network_tx)
        ? rate(m.network_rx + m.network_tx)
        : "Measuring…",
    note: (m) =>
      `${m.interface || "Interface unavailable"} · receive + transmit`,
    series: [
      { label: "Receive", value: (r) => r.network_rx },
      { label: "Transmit", value: (r) => r.network_tx },
    ],
    format: rate,
  },
  {
    key: "disk",
    title: "Disk",
    color: "warning",
    value: (m) => formatBytes(m.disk_used),
    note: (m) =>
      `${finite(m.disk_total) && finite(m.disk_used) ? formatBytes(m.disk_total - m.disk_used) : "Unavailable"} free · ${formatPercent(percentage(m.disk_used, m.disk_total))}`,
    series: [
      {
        label: "Disk used",
        value: (r) => percentage(r.disk_used, r.disk_total),
      },
    ],
    format: formatPercent,
    maximum: 100,
  },
];

function MetricPanel({
  definition: def,
  rows,
  windowName,
  measurement,
  unavailable,
}) {
  const theme = useTheme();
  const compact = useMediaQuery(theme.breakpoints.down("sm"));
  const id = useId();
  const [pinned, setPinned] = useState(null);
  const [hovered, setHovered] = useState(null);
  const selected = pinned || hovered;
  const width = compact ? 340 : 580;
  const left = def.key === "network" ? 90 : 54;
  const right = width - 12;
  const top = 12;
  const bottom = 140;
  const end = rows.at(-1)?.observed;
  const start = end - WINDOWS[windowName];
  const readings = rows
    .flatMap((row) => def.series.map((series) => series.value(row)))
    .filter(finite);
  const high = def.maximum || Math.max(1, ...readings) * 1.1;
  const x = (time) =>
    left + ((time - start) / WINDOWS[windowName]) * (right - left);
  const y = (value) =>
    bottom - Math.max(0, Math.min(1, value / high)) * (bottom - top);
  const color = (index) =>
    index ? theme.palette.success.main : theme.palette[def.color].main;
  const ready = rows.length >= 2 && readings.length > 0;
  const selectedVisible =
    selected && selected.observed >= start && selected.observed <= end;
  const describe = (row) =>
    `${clock(row.observed, true)} · ${def.series.map((series) => `${series.label}: ${def.format(series.value(row))}`).join(" · ")}`;

  function nearest(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    const position = Math.max(
      left,
      Math.min(right, ((event.clientX - rect.left) / rect.width) * width),
    );
    const time =
      start + ((position - left) / (right - left)) * WINDOWS[windowName];
    return rows.reduce(
      (best, row) =>
        Math.abs(row.observed - time) < Math.abs(best.observed - time)
          ? row
          : best,
      rows[0],
    );
  }
  function inspectKeyboard(event) {
    if (event.key === "Escape") {
      setPinned(null);
      setHovered(null);
      return;
    }
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let index = selected
      ? rows.findIndex((row) => row.observed >= selected.observed)
      : rows.length - 1;
    if (index < 0) index = rows.length - 1;
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = rows.length - 1;
    else
      index = Math.max(
        0,
        Math.min(
          rows.length - 1,
          index + (event.key === "ArrowRight" ? 1 : -1),
        ),
      );
    setPinned(rows[index]);
  }

  return (
    <Paper
      component="section"
      variant="outlined"
      aria-labelledby={`${id}-title`}
      sx={{ p: { xs: 2, sm: 2.5 }, minWidth: 0, borderRadius: 2 }}
    >
      <Typography component="h3" variant="h6" id={`${id}-title`}>
        {def.title}
      </Typography>
      <Typography
        component="p"
        sx={{
          fontSize: 25,
          fontWeight: 550,
          letterSpacing: "-.025em",
          mt: 1,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {unavailable ? "—" : def.value(measurement)}
      </Typography>
      <Typography
        color="text.secondary"
        sx={{ fontSize: 12, minHeight: 22, overflowWrap: "anywhere" }}
      >
        {unavailable
          ? "Current reading unavailable · retained history below"
          : def.note(measurement)}
      </Typography>
      <Box sx={{ mt: 2, minWidth: 0 }}>
        {ready ? (
          <Box
            component="svg"
            viewBox={`0 0 ${width} 178`}
            tabIndex={0}
            role="group"
            aria-label={`${def.title} history, ${windowName}. Use left and right arrows, Home or End to inspect samples. Escape resumes live.`}
            aria-describedby={`${id}-inspection`}
            onPointerMove={(event) => {
              if (!pinned && event.pointerType === "mouse")
                setHovered(nearest(event));
            }}
            onPointerDown={(event) => {
              setPinned(nearest(event));
              event.currentTarget.focus({ preventScroll: true });
            }}
            onPointerLeave={() => setHovered(null)}
            onBlur={() => setHovered(null)}
            onKeyDown={inspectKeyboard}
            sx={{
              width: "100%",
              display: "block",
              overflow: "hidden",
              touchAction: "pan-y",
              cursor: "crosshair",
              borderRadius: 1,
              "&:focus-visible": {
                outline: `2px solid ${theme.palette.primary.main}`,
                outlineOffset: 3,
              },
            }}
          >
            <title>{`${def.title} observed history`}</title>
            {[0, high / 2, high].map((value) => (
              <g key={value}>
                <line
                  x1={left}
                  x2={right}
                  y1={y(value)}
                  y2={y(value)}
                  stroke={theme.palette.divider}
                />
                <text
                  x={left - 8}
                  y={y(value) + 4}
                  textAnchor="end"
                  fill={theme.palette.text.secondary}
                  fontSize="11"
                >
                  {def.maximum ? `${Math.round(value)}%` : rate(value)}
                </text>
              </g>
            ))}
            <g opacity={unavailable ? 0.55 : 1}>
              {def.series.flatMap((series, index) =>
                splitSeries(
                  rows,
                  series.value,
                  Math.max(45, WINDOWS[windowName] / 60),
                ).map((segment, part) =>
                  segment.length === 1 ? (
                    <circle
                      key={`${index}-${part}`}
                      cx={x(segment[0].time)}
                      cy={y(segment[0].value)}
                      r="2"
                      fill={color(index)}
                    />
                  ) : (
                    <polyline
                      key={`${index}-${part}`}
                      points={segment
                        .map((point) => `${x(point.time)},${y(point.value)}`)
                        .join(" ")}
                      fill="none"
                      stroke={color(index)}
                      strokeWidth="1.8"
                      strokeLinejoin="round"
                      vectorEffect="non-scaling-stroke"
                    />
                  ),
                ),
              )}
            </g>
            {[start, end].map((time, index) => (
              <text
                key={index}
                x={index ? right : left}
                y="166"
                textAnchor={index ? "end" : "start"}
                fill={theme.palette.text.secondary}
                fontSize="11"
              >
                {new Date(time * 1000).toLocaleString("en-GB", {
                  ...(windowName === "24h"
                    ? { day: "2-digit", month: "short" }
                    : {}),
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </text>
            ))}
            {selectedVisible && (
              <g>
                <line
                  x1={x(selected.observed)}
                  x2={x(selected.observed)}
                  y1={top}
                  y2={bottom}
                  stroke={theme.palette.text.secondary}
                  strokeDasharray="3 3"
                />
                {def.series.map(
                  (series, index) =>
                    finite(series.value(selected)) && (
                      <circle
                        key={series.label}
                        cx={x(selected.observed)}
                        cy={y(series.value(selected))}
                        r="3.5"
                        fill={color(index)}
                        stroke={theme.palette.background.paper}
                        strokeWidth="1.5"
                      />
                    ),
                )}
              </g>
            )}
          </Box>
        ) : (
          <Box sx={{ minHeight: 160, display: "flex", alignItems: "center" }}>
            <Typography color="text.secondary" fontSize={13}>
              {!rows.length
                ? "No observed history in this window."
                : rows.length < 2
                  ? "Collecting history. The chart starts after two samples."
                  : "No measured values in this window. Missing samples are not plotted."}
            </Typography>
          </Box>
        )}
        <Stack direction="row" gap={2} flexWrap="wrap" sx={{ mt: 1 }}>
          {def.series.map((series, index) => (
            <Typography
              key={series.label}
              component="span"
              color="text.secondary"
              fontSize={12}
            >
              <Box
                component="span"
                sx={{
                  display: "inline-block",
                  width: 7,
                  height: 7,
                  borderRadius: "2px",
                  bgcolor: color(index),
                  mr: 0.75,
                }}
              />
              {series.label}
              {def.maximum ? " (%)" : " (bytes/s)"}
            </Typography>
          ))}
        </Stack>
        <Box
          id={`${id}-inspection`}
          sx={{ minHeight: 50, mt: 1.5, overflowWrap: "anywhere" }}
        >
          <Typography
            component="p"
            role="status"
            aria-live={pinned ? "polite" : "off"}
            color="text.secondary"
            fontSize={12}
          >
            {selected
              ? `${pinned ? "Inspecting" : "Sample"} · ${describe(selected)}${!selectedVisible ? " · Outside the displayed window" : ""}`
              : ready
                ? "Point or tap to inspect · arrow keys when focused"
                : "Only observed measurements are shown."}
          </Typography>
          {pinned && (
            <Button
              size="small"
              onClick={() => {
                setPinned(null);
                setHovered(null);
              }}
              sx={{ mt: 0.5 }}
            >
              Resume live view
            </Button>
          )}
        </Box>
      </Box>
    </Paper>
  );
}

export default function Telemetry({ data, connection = "live" }) {
  const [windowName, setWindowName] = useState("15m");
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);
  const observation = data?.snapshots?.telemetry;
  const measurement = observation?.value;
  const lastObserved = finite(measurement?.observed)
    ? measurement.observed
    : observation?.updated;
  const failed = Boolean(data?.snapshots?.["telemetry-error"]?.value?.error);
  const unavailable =
    !measurement ||
    !finite(lastObserved) ||
    now - lastObserved > 30 ||
    (finite(observation?.updated) && now - observation.updated > 30) ||
    failed ||
    connection !== "live";
  const rows = useMemo(
    () => normalizeHistory(data?.metric_history?.[windowName]),
    [data?.metric_history, windowName],
  );
  const warning = !measurement
    ? "Waiting for host measurements. The collector has not supplied a current reading."
    : failed
      ? "Host collection failed. Current readings are unavailable."
      : connection !== "live"
        ? "The live connection is unavailable. Current readings are paused."
        : "Host measurements are stale. Current readings are unavailable.";
  const alerts = !unavailable
    ? [
        measurement.cpu >= 90 && "CPU above 90%",
        percentage(measurement.memory_used, measurement.memory_total) >= 90 &&
          "Memory above 90%",
        percentage(measurement.disk_used, measurement.disk_total) >= 90 &&
          "Disk above 90%",
      ].filter(Boolean)
    : [];

  return (
    <Box component="section" aria-label="Host telemetry" sx={{ minWidth: 0 }}>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        gap={2}
        sx={{ mb: 2.5 }}
      >
        <Box>
          <Typography component="h2" variant="h6">
            Host resources
          </Typography>
          <Typography color="text.secondary" fontSize={12} sx={{ mt: 0.5 }}>
            {finite(lastObserved)
              ? `${unavailable ? "Last observed" : "Observed"} ${clock(lastObserved, true)}`
              : "Waiting for the host collector"}
          </Typography>
        </Box>
        <ToggleButtonGroup
          size="small"
          exclusive
          value={windowName}
          aria-label="History window"
          onChange={(_, value) => {
            if (value) setWindowName(value);
          }}
        >
          {Object.keys(WINDOWS).map((name) => (
            <ToggleButton
              key={name}
              value={name}
              aria-label={`Show ${name} history`}
              sx={{ px: 2, textTransform: "none" }}
            >
              {name}
            </ToggleButton>
          ))}
        </ToggleButtonGroup>
      </Stack>
      {unavailable && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {warning}
          {rows.length > 0 && " Retained history is shown below."}
        </Alert>
      )}
      {!!alerts.length && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {alerts.join(" · ")}
        </Alert>
      )}
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: {
            xs: "minmax(0, 1fr)",
            md: "repeat(2, minmax(0, 1fr))",
          },
          gap: 2,
        }}
      >
        {DEFINITIONS.map((definition) => (
          <MetricPanel
            key={definition.key}
            definition={definition}
            rows={rows}
            windowName={windowName}
            measurement={measurement}
            unavailable={unavailable}
          />
        ))}
      </Box>
      <Typography
        color="text.secondary"
        fontSize={12}
        sx={{ mt: 2, lineHeight: 1.8, overflowWrap: "anywhere" }}
      >
        History retained for 24 hours · recorded every 15 seconds · longer
        windows show sampled observations
        {!unavailable && finite(measurement.uptime)
          ? ` · Uptime ${Math.floor(measurement.uptime / 86400)}d ${Math.floor((measurement.uptime % 86400) / 3600)}h`
          : ""}
        {!unavailable &&
        finite(measurement.swap_used) &&
        finite(measurement.swap_total)
          ? ` · Swap ${formatBytes(measurement.swap_used)} / ${formatBytes(measurement.swap_total)}`
          : ""}
      </Typography>
    </Box>
  );
}
