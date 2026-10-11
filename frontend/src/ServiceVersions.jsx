import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, IconButton, Tooltip, Typography } from "@mui/material";
import Hint from "./Hint.jsx";
import { Refresh } from "./icons.jsx";
import { appendVersions, validateVersionPage, versionTimestamp } from "./serviceVersions.js";
import "./ServiceVersions.css";

export default function ServiceVersions({ project, environment, api, navigate, refreshKey }) {
  const [versions, setVersions] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [failedCursor, setFailedCursor] = useState(null);
  const generation = useRef(0);
  const label = environment === "production" ? "Production" : "Development";
  const load = useCallback(async (nextCursor = null) => {
    const request = ++generation.current;
    setLoading(true);
    setError("");
    if (!nextCursor) { setVersions([]); setCursor(null); setLoaded(false); setRefreshedAt(null); }
    try {
      const response = validateVersionPage(await api("action", {
        action: "service-versions", project, environment, limit: 25,
        ...(nextCursor ? { cursor: nextCursor } : {}),
      }), project, environment);
      if (request !== generation.current) return;
      setVersions(previous => nextCursor ? appendVersions(previous, response.versions) : response.versions);
      setCursor(response.next_cursor);
      setLoaded(true);
      setRefreshedAt(Date.now() / 1000);
    } catch (failure) {
      if (request !== generation.current) return;
      setFailedCursor(nextCursor);
      setError(failure.message || "Version history could not be loaded.");
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [api, project, environment]);
  useEffect(() => {
    load();
    return () => { generation.current += 1; };
  }, [load, refreshKey]);
  return <section className="service-versions" aria-label={`${label} versions`} aria-busy={loading}>
    <div className="section-heading versions-heading">
      <div className="versions-heading-copy">
      <div className="versions-title"><Typography component="h2" variant="h6">Versions</Typography>
      <Hint label="About version records">Service associations reflect saved definitions and bindings, not a live container inspection. Open a container above to inspect its observed image and state. Historical images may have been pruned and are not guaranteed to remain available.</Hint></div>
    <Typography className="versions-description" color="text.secondary" fontSize={13}>Recorded images published for {label.toLowerCase()}.</Typography>
      </div>
      <Tooltip title="Reload versions"><span><IconButton size="small" disabled={loading} onClick={() => load()} aria-label={`Reload ${label.toLowerCase()} versions`}><Refresh /></IconButton></span></Tooltip>
    </div>
    {error && <Alert severity="error" action={<Button onClick={() => load(failedCursor)}>Retry</Button>}>{error}</Alert>}
    {!!versions.length && <ol className="version-list">
      {versions.map(version => <li key={version.artifact_id} className="version-row">
        <div className="version-identity">
          <strong>{version.image_name || version.template_id || "Published image"}</strong>
          <div className="version-image-line"><code className="version-image">{version.image ? (version.image.length > 66 ? version.image.slice(0, 48) + "…" + version.image.slice(-12) : version.image) : "Image identity not recorded"}</code><Hint label="Full version identity"><span style={{ overflowWrap: "anywhere" }}>Image: <code>{version.image || "Not recorded"}</code><br />Artifact: <code>{version.artifact_id}</code><br />Commit: <code>{version.source_sha || "Not recorded"}</code></span></Hint></div>
        </div>
        <div className="version-source">
          {version.source_job ? <Button size="small" onClick={() => navigate("job/" + encodeURIComponent(version.source_job))}>Source run {String(version.source_job).slice(0, 12)}</Button> : <span>Source run not recorded</span>}
          <span>Commit <code>{version.source_sha?.slice(0, 9) || "Not recorded"}</code></span>
          <span>Published {versionTimestamp(version.published_at)}</span>
        </div>
        <div className="version-associations">
          <span className="version-associations-label">Recorded services</span>
          {version.recorded_services.length ? <ul>{version.recorded_services.map(service => <li key={service.service_id}>
            <strong>{service.name || service.service_id}</strong>
            <span>{(service.recorded_state || "State not recorded").replaceAll("_", " ").replaceAll("-", " ")}</span>
          </li>)}</ul> : <span>None recorded</span>}
        </div>
      </li>)}
    </ol>}
    {loading && <Typography role="status" color="text.secondary" fontSize={13} sx={{ py: 2 }}>{versions.length ? "Loading more versions…" : "Loading versions…"}</Typography>}
    {loaded && !loading && !versions.length && !error && <div className="versions-empty"><strong>No versions recorded</strong><Typography color="text.secondary" fontSize={13}>Images will appear here after a build publishes artifacts for this environment.</Typography></div>}
    {refreshedAt && <Typography color="text.secondary" fontSize={12} sx={{ mt: 1 }}>History refreshed {versionTimestamp(refreshedAt)}</Typography>}
    {cursor && !error && <div className="versions-pagination"><Button variant="outlined" size="small" disabled={loading} onClick={() => load(cursor)}>Load more versions</Button><span>{versions.length} versions shown</span></div>}
  </section>;
}
