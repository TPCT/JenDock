import TextField from "./Field.jsx";
import {
  WarningIcon,
  Info,
  PlayArrow,
  GridView,
  Folder,
  History,
  Terminal,
  Settings,
  Search,
  Menu,
  Close,
  Arrow,
  Refresh,
  Rocket,
  Check,
  SelectionCheck,
  Branch,
  Chevron,
  Dns,
  Dark,
  Light,
  Logout,
  Visibility,
  VisibilityOff,
  ErrorIcon,
  PendingIcon,
  SkippedIcon,
  Workflow,
} from "./icons.jsx";
import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  createContext,
  useContext,
  lazy,
  Suspense,
} from "react";
import { createRoot } from "react-dom/client";
import {
  ThemeProvider,
  createTheme,
  CssBaseline,
  Box,
  Typography,
  Button,
  IconButton,
  Tooltip,
  Stack,
  Tabs,
  Tab,
  Autocomplete,
  Switch,
  FormControlLabel,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Divider,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Snackbar,
  Alert,
  Breadcrumbs,
  Link,
  Avatar,
  InputAdornment,
  CircularProgress,
  Menu as ActionMenu,
  MenuItem,
  RadioGroup,
  Radio,
  useMediaQuery,
} from "@mui/material";
import Resources from "./Resources.jsx";
import ServiceCreation from "./ServiceCreation.jsx";
import ServiceVersions from "./ServiceVersions.jsx";
import { clearServiceDrafts } from "./serviceCreation.js";
import BuildProfile from "./BuildProfile.jsx";
import EnvironmentSettings from "./EnvironmentSettings.jsx";
import AdoptWorkload from "./AdoptWorkload.jsx";
import ProjectCreateDialog from "./ProjectCreateDialog.jsx";
import Hint from "./Hint.jsx";
import { storeCreation, restoreCreation, clearCreation, creationSessionStorage } from "./projectCreationStorage.js";
import {
  workspaceEnvironment,
  containerRoute,
  projectReturnRoute,
  projectTabIndex,
  activeStates,
  statusLabel,
  date,
  servicesFor,
  stale,
  branchState,
  buildPayload,
} from "./model.js";
import "./style.css";
import "./github.css";
import "./spacing.css";
import Domains from "./Domains.jsx";
import "./Domains.css";
const ContainerPage = deferredScreen(() => import("./ContainerPage.jsx"), "container");
const RunDetail = deferredScreen(() => import("./RunDetail.jsx"), "deployment");
const Telemetry = deferredScreen(() => import("./Telemetry.jsx"), "telemetry");
const IntegrationsPage = deferredScreen(() => import("./IntegrationsPage.jsx"), "integrations");
// Define once so polling and parent renders preserve each screen's state.
function deferredScreen(load, name) {
  const loadModule = async () => {
    try { return await load(); }
    catch (cause) {
      const failure = new Error('Screen module could not be downloaded.', { cause });
      failure.screenDownloadFailed = true;
      throw failure;
    }
  };
  return class DeferredScreen extends React.Component {
    state = { failed: false, downloadFailed: false };
    Screen = lazy(loadModule);
    static getDerivedStateFromError(error) { return { failed: true, downloadFailed: error?.screenDownloadFailed === true }; }
    retry = () => {
      this.Screen = lazy(loadModule);
      this.setState({ failed: false, downloadFailed: false });
    };
    render() {
      if (this.state.downloadFailed) return <Alert severity="error" action={<Button color="inherit" onClick={() => window.location.reload()}>Reload application</Button>}>The {name} screen could not be downloaded. Reload the application to get the current screen files.</Alert>;
      if (this.state.failed) return <Alert severity="error" action={<Button color="inherit" onClick={this.retry}>Retry</Button>}>The {name} screen could not open. Try again.</Alert>;
      const Screen = this.Screen;
      return <Suspense fallback={<Stack direction="row" spacing={2} alignItems="center" role="status" sx={{ py: "var(--space-panel)" }}><CircularProgress size={20} aria-hidden="true"/><Typography>Loading {name}…</Typography></Stack>}><Screen {...this.props}/></Suspense>;
    }
  };
}
const Context = createContext(null);
const useApp = () => useContext(Context);
const label = (data, id) => data.projects[id]?.label || id;
function IconAction({ label: caption, icon: I, onClick, disabled = false }) {
  return (
    <Tooltip title={caption} enterTouchDelay={0}>
      <span>
        <IconButton aria-label={caption} onClick={onClick} disabled={disabled}>
          <I sx={{ fontSize: 18 }} />
        </IconButton>
      </span>
    </Tooltip>
  );
}
function BranchPicker({ label, error, helperText, onRefresh, ...props }) {
  const [refreshing, setRefreshing] = useState(false);
  return (
    <Autocomplete
      {...props}
      disableClearable
      className="branch-picker"
      renderInput={(params) => (
        <TextField
          {...params}
          label={label}
          error={error}
          helperText={helperText}
          slotProps={{
            input: {
              ...params.InputProps,
              endAdornment: (
                <div className="branch-picker-actions">
                  <Tooltip title="Refresh branches">
                    <span>
                      <IconButton
                        aria-label="Refresh branches"
                        disabled={props.disabled || refreshing}
                        onClick={async () => {
                          setRefreshing(true);
                          try {
                            await onRefresh();
                          } finally {
                            setRefreshing(false);
                          }
                        }}
                      >
                        {refreshing ? (
                          <CircularProgress size={18} />
                        ) : (
                          <Refresh sx={{ fontSize: 19 }} />
                        )}
                      </IconButton>
                    </span>
                  </Tooltip>
                  {params.InputProps.endAdornment}
                </div>
              ),
            },
          }}
        />
      )}
    />
  );
}
function Status({ value }) {
  const good = ["healthy", "succeeded", "validated"].includes(
      String(value).toLowerCase(),
    ),
    bad = ["unhealthy", "failed", "interrupted", "dead"].includes(
      String(value).toLowerCase(),
    );
  return (
    <span
      className={
        "health " + (bad ? "health-bad" : good ? "" : "health-neutral")
      }
    >
      {good ? (
        <Check sx={{ fontSize: 16 }} />
      ) : bad ? (
        <ErrorIcon sx={{ fontSize: 16 }} />
      ) : String(value).toLowerCase() === "running" ? (
        <PlayArrow sx={{ fontSize: 16 }} />
      ) : ["cancelled", "superseded", "exited"].includes(value) ? (
        <SkippedIcon sx={{ fontSize: 16 }} />
      ) : (
        <PendingIcon sx={{ fontSize: 16 }} />
      )}
      {statusLabel(value)}
    </span>
  );
}
function Empty({ title, children }) {
  return (
    <Box className="empty-state">
      <Typography variant="h6">{title}</Typography>
      {children && <Typography component="div" color="text.secondary" className="empty-state-description">
        {children}
      </Typography>}
    </Box>
  );
}
function App() {
  const creationDraft = useRef(null);
  const creationOwner = useRef(null);
  const [session, setSessionState] = useState(null),
    [boot, setBoot] = useState(true),
    [data, setData] = useState(null),
    [error, setError] = useState(""),
    [connection, setConnection] = useState("connecting"),
    [toast, setToast] = useState(""),
    [route, setRoute] = useState(location.hash.slice(1) || "overview"),
    [drawer, setDrawer] = useState(false),
    [night, setNight] = useState(
      () => localStorage.getItem("monitor-theme") === "dark",
    ),
    [build, setBuild] = useState(null),
    [pendingRoute, setPendingRoute] = useState(null),
    [dirty, setDirty] = useState(false),
    [operationPending, setOperationPending] = useState(false),
    [clock, setClock] = useState(0);
  const sessionRef = useRef(session),
    routeRef = useRef(route),
    dirtyRef = useRef(false),
    pendingRef = useRef(false),
    fetchSeq = useRef(0);
  const setSession = useCallback(value => {
    if (value?.username) {
      creationOwner.current = value.username;
      creationDraft.current = restoreCreation(creationSessionStorage(), value.username);
    } else {
      clearCreation(creationSessionStorage(), creationOwner.current);
      clearServiceDrafts(creationSessionStorage(), creationOwner.current);
      creationOwner.current = null;
      creationDraft.current = null;
    }
    setSessionState(value);
  }, []);
  const saveCreationDraft = useCallback(value => {
    if (!creationOwner.current) return;
    creationDraft.current = value;
    storeCreation(creationSessionStorage(), creationOwner.current, value);
  }, []);
  sessionRef.current = session;
  routeRef.current = route;
  dirtyRef.current = dirty;
  pendingRef.current = operationPending;
  const mobile = useMediaQuery("(max-width:800px)");
  const api = useCallback(async (path, body) => {
    let r;
    try {
      r = await fetch("./api/" + path, {
        cache: "no-store",
        credentials: "same-origin",
        ...(body
          ? {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": sessionRef.current?.csrf || "",
              },
              body: JSON.stringify(body),
            }
          : {}),
      });
    } catch {
      throw Error("Unable to connect. Check your connection before retrying.");
    }
    let v;
    try {
      v = await r.json();
    } catch {
      throw Error("The server returned an unreadable response.");
    }
    if (!r.ok) {
      if (r.status === 401 && path !== "login") {
        setSession(null);
        setData(null);
        setBuild(null);
        setDirty(false);
      }
      const failure = Error(v.error || "Request failed.");
      failure.status = r.status;
      failure.code = v.code;
      if (v.outcome === "not_committed") failure.outcome = "not_committed";
      throw failure;
    }
    return v;
  }, []);
  const refresh = useCallback(async () => {
    const seq = ++fetchSeq.current;
    try {
      const value = await api("state");
      if (seq === fetchSeq.current) {
        setData(value);
        setError("");
      }
      return value;
    } catch (e) {
      if (seq === fetchSeq.current) setError(e.message);
      throw e;
    }
  }, [api]);
  useEffect(() => {
    api("session")
      .then((s) => {
        setSession(s);
      })
      .catch(() => {})
      .finally(() => setBoot(false));
  }, [api]);
  useEffect(() => {
    if (!session) return;
    refresh().catch(() => {});
    const events = new EventSource("./api/events");
    events.addEventListener("state", (e) => {
      try {
        fetchSeq.current++;
        setData(JSON.parse(e.data));
        setError("");
        setConnection("live");
      } catch {
        setConnection("reconnecting");
      }
    });
    events.addEventListener("expired", () => {
      setSession(null);
      setData(null);
      setBuild(null);
      setDirty(false);
    });
    events.onopen = () => setConnection("live");
    events.onerror = () => setConnection("reconnecting");
    const timer = setInterval(() => {
      setClock((n) => n + 1);
      refresh().catch(() => {});
    }, 15000);
    return () => {
      events.close();
      clearInterval(timer);
    };
  }, [session, api, refresh]);
  useEffect(() => {
    const change = () => {
      const next = location.hash.slice(1) || "overview";
      if (pendingRef.current && next !== routeRef.current) {
        history.replaceState(null, "", "#" + routeRef.current);
        setToast("Wait for the current operation to finish.");
        return;
      }
      if (dirtyRef.current && next !== routeRef.current) {
        history.replaceState(null, "", "#" + routeRef.current);
        setPendingRoute(next);
      } else {
        setRoute(next);
        setDrawer(false);
        window.scrollTo(0, 0);
      }
    };
    const unload = (e) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("hashchange", change);
    window.addEventListener("beforeunload", unload);
    return () => {
      window.removeEventListener("hashchange", change);
      window.removeEventListener("beforeunload", unload);
    };
  }, []);
  const theme = createTheme({
    palette: {
      mode: night ? "dark" : "light",
      primary: { main: night ? "#4493f8" : "#0969da" },
      background: {
        default: night ? "#0d1117" : "#fff",
        paper: night ? "#161b22" : "#fff",
      },
      text: {
        primary: night ? "#f0f6fc" : "#1f2328",
        secondary: night ? "#9198a1" : "#59636e",
      },
      divider: night ? "#3d444d" : "#d1d9e0",
      success: { main: night ? "#3fb950" : "#1a7f37" },
    },
    typography: {
      fontFamily: "Inter, sans-serif",
      fontSize: 14,
      button: { textTransform: "none", fontWeight: 550 },
      h4: { fontSize: 26, fontWeight: 600, letterSpacing: "-.025em" },
      h6: { fontSize: 16, fontWeight: 600 },
    },
    shape: { borderRadius: 6 },
    components: {
      // Gap keeps wrapped toolbars and responsive form stacks evenly spaced.
      MuiStack: { defaultProps: { useFlexGap: true } },
      MuiAutocomplete: {
        defaultProps: {
          selectOnFocus: false,
          autoHighlight: true,
          renderOption: (props, option, { selected }, ownerState) => {
            const { key, ...itemProps } = props;
            return <li key={key} {...itemProps}><span className="select-option-check" aria-hidden="true"><SelectionCheck sx={{fontSize:16,visibility:selected?"visible":"hidden"}}/></span><span className="select-option-label">{ownerState.getOptionLabel(option)}</span></li>;
          },
        },
      },
      MuiFormLabel: { styleOverrides: { asterisk: { display: "none" } } },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: { minHeight: 36, fontSize: 14 },
          containedPrimary: {
            backgroundColor: night ? "#238636" : "#1f883d",
            color: "#fff",
            border: "1px solid #1f232826",
            "&:hover": { backgroundColor: night ? "#29903b" : "#1a7f37" },
          },
          outlined: {
            color: "inherit",
            borderColor: night ? "#3d444d" : "#d1d9e0",
            backgroundColor: night ? "#212830" : "#f6f8fa",
            "&:hover": {
              backgroundColor: night ? "#2a313c" : "#eff2f5",
              borderColor: night ? "#59636e" : "#b7c0ca",
            },
          },
        },
      },
      MuiIconButton: { styleOverrides: { root: { width: 40, height: 40 } } },
      MuiTableCell: {
        styleOverrides: {
          root: { padding: "var(--space-group) var(--space-panel)" },
          sizeSmall: { padding: "var(--space-inline) var(--space-panel)" },
        },
      },
      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: "none",
            fontSize: 13,
            minWidth: 0,
            padding: "12px 0",
            marginRight: 24,
          },
        },
      },
      MuiStack: { defaultProps: { useFlexGap: true } },
      MuiTooltip: { defaultProps: { enterTouchDelay: 0 } },
      MuiFormControlLabel: {
        styleOverrides: {
          root: { margin: 0, gap: "var(--space-tight)", alignItems: "center" },
          label: { paddingBlock: "var(--space-inline)" },
        },
      },
      MuiAlert: {
        styleOverrides: {
          root: { padding: "var(--space-group) var(--space-panel)", gap: "var(--space-group)", alignItems: "flex-start" },
          icon: { margin: 0, padding: 0, minHeight: 24, alignItems: "center" },
          message: { padding: 0, minWidth: 0, lineHeight: "24px" },
          action: { margin: 0, padding: 0, alignSelf: "center" },
        },
        defaultProps: {
          iconMapping: {
            error: <ErrorIcon />,
            success: <Check />,
            warning: <WarningIcon />,
            info: <Info />,
          },
        },
      },
      MuiPaper: { defaultProps: { elevation: 0 } },
      MuiDialog: {
        defaultProps: { scroll: "paper" },
        styleOverrides: {
          paper: {
            borderRadius: 14,
            maxHeight: "calc(100dvh - 32px)",
            margin: 16,
          },
        },
      },
      MuiDialogTitle: {
        styleOverrides: {
          root: ({ theme }) => ({
            padding: "var(--space-panel) var(--space-page)",
            borderBottom: `1px solid ${theme.palette.divider}`,
            flexShrink: 0,
            fontSize: 20,
            fontWeight: 600,
          }),
        },
      },
      MuiDialogContent: {
        styleOverrides: {
          root: {
            padding: "var(--space-panel) var(--space-page)",
            "&.MuiDialogContent-root": { paddingTop: "var(--space-panel)" },
            overflowY: "auto",
          },
        },
      },
      MuiDialogActions: {
        styleOverrides: {
          root: ({ theme }) => ({
            padding: "var(--space-group) var(--space-page)",
            borderTop: `1px solid ${theme.palette.divider}`,
            flexShrink: 0,
            flexWrap: "wrap",
            gap: "var(--space-inline)",
            "& > :not(style) ~ :not(style)": { marginLeft: 0 },
          }),
        },
      },
    },
  });
  function navigate(next) {
    if (operationPending) {
      setToast("Wait for the current operation to finish.");
      return;
    }
    if (dirty) {
      setPendingRoute(next);
      return;
    }
    location.hash = next;
  }
  const [path, query] = route.split("?");
  const [page, rawId, rawName] = path.split("/");
  let id = "",
    name = "";
  try {
    id = decodeURIComponent(rawId || "");
    name = decodeURIComponent(rawName || "");
  } catch {}
  const currentProject = data?.projects[id],
    jobs = data?.jobs || [],
    job = page === "job" ? jobs.find((j) => j.id === Number(id)) : null;
  const currentId = job?.project || id;
  const onAction = async (body) => {
    const r = await api("action", body);
    setToast(r.message || "Saved");
    await refresh();
    return r;
  };
  const context = {
    username: session?.username,
    creationDraft,
    saveCreationDraft,
    data,
    api,
    refresh,
    notify: setToast,
    navigate,
    openBuild: (project, retry = null, target = null) => setBuild({ project, retry, target }),
    action: onAction,
    connection,
    observationFailed: !!error,
    setDirty,
    setOperationPending,
  };

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Context.Provider value={context}>
        {boot ? (
          <Box className="boot-state">
            <CircularProgress />
            <Typography>Checking your session…</Typography>
          </Box>
        ) : !session ? (
          <Login api={api} onLogin={setSession} />
        ) : !data ? (
          <Box className="boot-state">
            {error ? (
              <>
                <Alert severity="error">{error}</Alert>
                <Button onClick={() => refresh().catch(() => {})}>
                  Try again
                </Button>
              </>
            ) : (
              <>
                <CircularProgress />
                <Typography>Loading workspace…</Typography>
              </>
            )}
          </Box>
        ) : (
          <div className={"premium " + (night ? "night" : "")}>
            <aside className="nav">
              <Nav
                {...{
                  data,
                  jobs,
                  page,
                  currentId,
                  navigate,
                  session,
                  api,
                  setSession,
                  setData,
                  setDirty,
                  setToast,
                  dirty,
                  operationPending,
                }}
              />
            </aside>
            <Drawer open={drawer} onClose={() => setDrawer(false)}>
              <div className={"mobile-nav " + (night ? "night" : "")}>
                <IconAction
                  label="Close navigation"
                  icon={Close}
                  onClick={() => setDrawer(false)}
                />
                <Nav
                  {...{
                    data,
                    jobs,
                    page,
                    currentId,
                    navigate,
                    session,
                    api,
                    setSession,
                    setData,
                    setDirty,
                    setToast,
                    dirty,
                    operationPending,
                  }}
                />
              </div>
            </Drawer>
            <div className="workspace-main">
              <div className="workspace-top">
                <div className="mobile-menu">
                  <IconAction
                    label="Open navigation"
                    icon={Menu}
                    onClick={() => setDrawer(true)}
                  />
                </div>
                <Breadcrumbs separator={<Chevron sx={{ fontSize: 14 }} />}>
                  {["project", "container", "job"].includes(page) &&
                    !mobile && (
                      <Link
                        component="button"
                        color="text.secondary"
                        underline="hover"
                        onClick={() =>
                          navigate(page === "job" ? "jobs" : "projects")
                        }
                      >
                        {page === "job" ? "Deployments" : "Projects"}
                      </Link>
                    )}
                  {page === "container" && (
                    <Link
                      component="button"
                      color="text.secondary"
                      underline="hover"
                      onClick={() =>
                        navigate(projectReturnRoute(id, new URLSearchParams(query || "").get("environment")))
                      }
                    >
                      {label(data, id)}{workspaceEnvironment(new URLSearchParams(query || "").get("environment")) && " · " + (new URLSearchParams(query).get("environment") === "production" ? "Production" : "Development")}
                    </Link>
                  )}
                  <Typography noWrap fontSize={12}>
                    {page === "container"
                      ? name
                      : page === "project"
                        ? label(data, id)
                        : page === "job"
                          ? "Run #" + id
                          : {
                              overview: "Overview",
                              projects: "Projects",
                              jobs: "Deployments",
                              containers: "Containers",
                              integrations: "Integrations",
                              audit: "Activity",
                            }[page] || "Workspace"}
                  </Typography>
                </Breadcrumbs>
                <div className="top-right">
                  <Tooltip title={date(data.snapshots.containers?.updated)}>
                    <span className="connection-label">
                      {data.preview
                        ? "Captured preview"
                        : connection === "live" && !error
                          ? "Live updates"
                          : "Reconnecting…"}
                    </span>
                  </Tooltip>
                  <IconAction
                    label={night ? "Light appearance" : "Dark appearance"}
                    icon={night ? Light : Dark}
                    onClick={() => {
                      setNight(!night);
                      localStorage.setItem(
                        "monitor-theme",
                        night ? "light" : "dark",
                      );
                    }}
                  />
                </div>
              </div>
              <main>
                {data.preview && (
                  <Alert severity="info" sx={{ mb: 3 }}>
                    Local preview uses captured observations. Production
                    operations are disconnected.
                  </Alert>
                )}
                {(error || stale(data, false)) && (
                  <Alert
                    severity="warning"
                    sx={{ mb: 3 }}
                    action={
                      <Button
                        color="inherit"
                        onClick={() => refresh().catch(() => {})}
                      >
                        Retry
                      </Button>
                    }
                  >
                    {error ||
                      "Service observations are stale. Check the collector before changing services."}
                  </Alert>
                )}
                {page === "project" ? (
                  <ProjectPage key={id} id={id}
                    environment={new URLSearchParams(query || "").get("environment") || "development"}
                    initialTab={new URLSearchParams(query || "").get("tab")}
                    onTabChange={tab => {
                      const params = new URLSearchParams(location.hash.split("?")[1] || "");
                      params.set("tab", tab);
                      const next = "project/" + encodeURIComponent(id) + "?" + params.toString();
                      history.replaceState(null, "", "#" + next);
                      setRoute(next);
                    }}
                    onEnvironmentChange={environment => {
                      const params = new URLSearchParams(location.hash.split("?")[1] || "");
                      params.set("environment", environment);
                      const next = "project/" + encodeURIComponent(id) + "?" + params.toString();
                      navigate(next);
                    }} />
                ) : page === "container" ? (
                  <ContainerPage
                    username={session?.username}
                    key={id + "/" + name}
                    project={id}
                    name={name}
                    data={data}
                    api={api}
                    refresh={refresh}
                    notify={setToast}
                    setDirty={setDirty}
                    setOperationPending={setOperationPending}
                    observationFailed={!!error}
                    initialTab={new URLSearchParams(query || "").get("tab")}
                    environment={new URLSearchParams(query || "").get("environment")}
                  />
                ) : page === "jobs" ? (
                  <RunsPage />
                ) : page === "job" ? (
                  <RunPage id={id} />
                ) : page === "projects" ? (
                  <ProjectsPage />
                ) : page === "containers" ? (
                  <ServicesPage />
                ) : page === "integrations" ? (
                  <IntegrationsPage api={api} setDirty={setDirty} setOperationPending={setOperationPending}/>
                ) : page === "audit" ? (
                  <Activity />
                ) : page === "overview" ? (
                  <Overview />
                ) : (
                  <Empty title="Page not found">
                    <Button onClick={() => navigate("projects")}>
                      View projects
                    </Button>
                  </Empty>
                )}
                <footer>
                  Shiftcodes · Observed{" "}
                  {date(data.snapshots.containers?.updated)}
                </footer>
              </main>
            </div>
          </div>
        )}
        {build && session && data && (
          <BuildDialog context={build} onClose={() => setBuild(null)} />
        )}
        <Dialog open={!!pendingRoute} onClose={() => setPendingRoute(null)}>
          <DialogTitle>Discard unsaved changes?</DialogTitle>
          <DialogContent>
            Your changes have not been applied. Stay to review them, or discard
            this draft.
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setPendingRoute(null)}>Stay</Button>
            <Button
              color="error"
              onClick={() => {
                setDirty(false);
                dirtyRef.current = false;
                location.hash = pendingRoute;
                setPendingRoute(null);
              }}
            >
              Discard changes
            </Button>
          </DialogActions>
        </Dialog>
        <Snackbar
          open={!!toast}
          autoHideDuration={6000}
          message={toast}
          onClose={() => setToast("")}
        />
      </Context.Provider>
    </ThemeProvider>
  );
}
function Nav({
  data,
  jobs,
  page,
  currentId,
  navigate,
  session,
  api,
  setSession,
  setData,
  setDirty,
  setToast,
  dirty,
  operationPending,
}) {
  return (
    <div className="nav-content">
      <div className="wordmark">
        <Workflow sx={{ fontSize: 26 }} />
        shiftcodes
      </div>
      <button className="workspace-switch" onClick={() => navigate("overview")}>
        <Avatar
          variant="rounded"
          sx={{
            width: 28,
            height: 28,
            fontSize: 11,
            bgcolor: "action.selected",
            color: "text.primary",
          }}
        >
          TP
        </Avatar>
        <span>
          TPCT workspace<small>Production infrastructure</small>
        </span>
        <Chevron fontSize="small" />
      </button>
      <button className="quick-find" onClick={() => navigate("projects")}>
        <Search fontSize="small" />
        Find a project
        <Chevron sx={{ fontSize: 16, marginLeft: "auto" }} />
      </button>
      <List className="nav-list">
        {[
          ["overview", "Overview", GridView],
          ["projects", "Projects", Folder],
          ["jobs", "Deployments", Workflow],
          ["containers", "Containers", Dns],
          ["integrations", "Integrations", Settings],
          ["audit", "Activity", History],
        ].map(([key, title, I]) => (
          <ListItemButton
            key={key}
            selected={
              key === page ||
              (key === "projects" && ["project", "container"].includes(page)) ||
              (key === "jobs" && page === "job")
            }
            onClick={() => navigate(key)}
          >
            <ListItemIcon>
              <I sx={{ fontSize: 18 }} />
            </ListItemIcon>
            <ListItemText primary={title} />
            {key === "jobs" &&
              jobs.some((j) => activeStates.includes(j.status)) && (
                <span className="nav-counter">
                  {jobs.filter((j) => activeStates.includes(j.status)).length}
                </span>
              )}
          </ListItemButton>
        ))}
      </List>
      <div className="nav-divider" />
      <div className="nav-label">
        Projects<span>{Object.keys(data.projects).length}</span>
      </div>
      <List className="project-nav">
        {Object.entries(data.projects).map(([key, p], i) => (
          <ListItemButton
            key={key}
            selected={key === currentId}
            onClick={() => navigate("project/" + encodeURIComponent(key))}
          >
            <Folder sx={{ fontSize: 16, mr: 1.5, color: "text.secondary" }} />
            <ListItemText
              primary={p.label || key}
              primaryTypographyProps={{ noWrap: true, title: p.label || key }}
            />
          </ListItemButton>
        ))}
      </List>
      <div className="owner">
        <Avatar
          sx={{ width: 28, height: 28, fontSize: 11, bgcolor: "#525264" }}
        >
          {session.username.slice(0, 2).toUpperCase()}
        </Avatar>
        <span>{session.username}</span>
        <IconAction
          label="Sign out"
          icon={Logout}
          onClick={async () => {
            if (dirty || operationPending) {
              setToast(
                "Save or discard your changes and wait for pending operations before signing out.",
              );
              return;
            }
            try {
              await api("action", { action: "logout" });
              setSession(null);
              setData(null);
              setDirty(false);
            } catch (e) {
              setToast(e.message);
            }
          }}
        />
      </div>
    </div>
  );
}
function Login({ api, onLogin }) {
  const [user, setUser] = useState(""),
    [password, setPassword] = useState(""),
    [show, setShow] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [attempted, setAttempted] = useState(false);
  return (
    <Box
      className="login-page"
      sx={{
        bgcolor: (theme) =>
          theme.palette.mode === "dark" ? "#0d1117" : "#f6f8fa",
      }}
    >
      <header className="login-header">
        <div className="wordmark">
          <Workflow sx={{ fontSize: 26 }} />
          shiftcodes
        </div>
        <Typography
          className="login-product"
          variant="body2"
          color="text.secondary"
        >
          Deployment console
        </Typography>
      </header>
      <main className="login-main">
        <Box
          component="form"
          className="login-form"
          sx={{
            bgcolor: "background.paper",
            borderColor: "divider",
            "@media (max-width: 800px)": {
              bgcolor: "transparent",
              borderRadius: 0,
            },
            "& input::placeholder": { opacity: 1, color: "text.secondary" },
          }}
          noValidate
          onSubmit={async (e) => {
            e.preventDefault();
            if (busy) return;
            setAttempted(true);
            setError("");
            if (!user.trim() || !password) {
              e.currentTarget
                .querySelector(
                  !user.trim() ? "#login-username" : "#login-password",
                )
                ?.focus();
              return;
            }
            setBusy(true);
            setError("");
            try {
              onLogin(await api("login", { username: user.trim(), password }));
              setPassword("");
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="login-intro">
            <Typography component="h1">Sign in to Shiftcodes</Typography>
            <Typography color="text.secondary">
              Your projects, deployments and containers.
            </Typography>
          </div>
          <Stack spacing={2.5}>
            <div className="login-field">
              <Typography component="label" htmlFor="login-username">
                Username
              </Typography>
              <TextField
                id="login-username"
                placeholder="Enter your username"
                fullWidth
                disabled={busy}
                slotProps={{
                  htmlInput: { autoCapitalize: "none", spellCheck: false },
                }}
                autoComplete="username"
                required
                error={attempted && !user.trim()}
                helperText={
                  attempted && !user.trim() ? "Enter your username." : undefined
                }
                value={user}
                onChange={(e) => setUser(e.target.value)}
              />
            </div>
            <div className="login-field">
              <Typography component="label" htmlFor="login-password">
                Password
              </Typography>
              <TextField
                id="login-password"
                placeholder="Enter your password"
                fullWidth
                disabled={busy}
                error={attempted && !password}
                helperText={
                  attempted && !password ? "Enter your password." : undefined
                }
                type={show ? "text" : "password"}
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                slotProps={{
                  input: {
                    endAdornment: (
                      <InputAdornment position="end">
                        <IconAction
                          label={show ? "Hide password" : "Show password"}
                          icon={show ? VisibilityOff : Visibility}
                          onClick={() => setShow(!show)}
                          disabled={busy}
                        />
                      </InputAdornment>
                    ),
                  },
                }}
              />
            </div>
            {error && <Alert severity="error">{error}</Alert>}
            <Button
              type="submit"
              variant="contained"
              disabled={busy}
              className="login-submit"
              endIcon={
                busy ? (
                  <CircularProgress size={18} color="inherit" />
                ) : (
                  <Arrow />
                )
              }
            >
              {busy ? "Signing in…" : "Login"}
            </Button>
          </Stack>
        </Box>
        <Typography className="login-access" color="text.secondary">
          Access is restricted to authorized operators.
        </Typography>
      </main>
      <footer className="login-footer">
        <Typography variant="caption" color="text.secondary">
          Shiftcodes · Infrastructure workspace
        </Typography>
      </footer>
    </Box>
  );
}
function Services({ id, items, environment, accessBlockedReason = "" }) {
  const { navigate, observationFailed, data } = useApp();
  const old = stale(data, observationFailed);
  if (!items.length)
    return (
      <Empty title="No containers observed">
        Check the project mapping or wait for the collector.
      </Empty>
    );
  return (
    <div className="service-table">
      <div className="table-head">
        <span>Container / image</span>
        <span>State / health</span>
        <span>CPU / memory</span>
        <span>Access</span>
      </div>
      {items.map((c) => {
        const unavailable = old || !!accessBlockedReason || c.capabilities?.manage === false;
        const reason = accessBlockedReason || (old
          ? "Container observations are stale. Wait for a fresh observation before opening a terminal. You can still inspect container details."
          : c.capabilities?.reason || "Container management is unavailable until ownership is verified. You can still inspect container details.");
        return (
        <div className="service-line" key={c.Names}>
          <div className="service-name">
            <div className="service-symbol">
              <Dns sx={{ fontSize: 19 }} />
            </div>
            <div>
              <Link
                component="button"
                color="text.primary"
                underline="hover"
                onClick={() =>
                  id &&
                  navigate(
                    containerRoute(id, c.Names, environment),
                  )
                }
              >
                {c.Names}
              </Link>
              <small className="wrap-anywhere">{c.Image}</small>
            </div>
          </div>
          <div>
            <Status value={old ? "Stale" : c.State} />
            <small className="health-caption">
              {c.Status?.includes("unhealthy")
                ? "Unhealthy"
                : c.Status?.includes("(healthy)")
                  ? "Health check passed"
                  : "No passing health check recorded"}
            </small>
          </div>
          <span className="resource-number">
            {old ? "—" : c.CPUPerc || "—"}
            <small>{old ? "—" : c.MemUsage || "—"}</small>
          </span>
          <div className="service-actions">
            {id ? (
              <>
                <IconAction
                  label={"Terminal · " + c.Names}
                  icon={Terminal}
                  disabled={unavailable}
                  onClick={() =>
                    navigate(
                      containerRoute(id, c.Names, environment, "terminal"),
                    )
                  }
                />
                <IconAction
                  label={(unavailable ? "Inspect · " : "Manage · ") + c.Names}
                  icon={Settings}
                  onClick={() =>
                    navigate(
                      containerRoute(id, c.Names, environment),
                    )
                  }
                />
                {unavailable && <Hint label={"Why terminal is unavailable · " + c.Names}>{reason}</Hint>}
              </>
            ) : (
              <Typography variant="caption">Observed only</Typography>
            )}
          </div>
        </div>
        );
      })}
    </div>
  );
}
function ProjectPage({ id, environment: requestedEnvironment = "development", initialTab, onEnvironmentChange, onTabChange }) {
  const { data, navigate, openBuild, api, refresh, setDirty, setOperationPending, username } = useApp();
  const environment = requestedEnvironment === "production" ? "production" : "development";
  const environmentLabel = environment === "production" ? "Production" : "Development";
  const [tab, setTab] = useState(projectTabIndex(initialTab)), [workspace, setWorkspace] = useState(null), [workspaceError, setWorkspaceError] = useState(""), [loading, setLoading] = useState(true), [reload, setReload] = useState(0), [legacyView, setLegacyView] = useState(null), [budgetsOpen, setBudgetsOpen] = useState(false), [budgetsViewed, setBudgetsViewed] = useState(false), [settingsDirty, setSettingsDirty] = useState(false), [resourcesDirty, setResourcesDirty] = useState(false), [environmentsDirty, setEnvironmentsDirty] = useState(false), [settingsPending, setSettingsPending] = useState(false), [resourcesPending, setResourcesPending] = useState(false), [environmentsPending, setEnvironmentsPending] = useState(false);
  useEffect(() => { setTab(projectTabIndex(initialTab)); }, [initialTab]);
  const p = data.projects[id];
  const [linkedProject,setLinkedProject]=useState(null);
  const [resourceFooter,setResourceFooter]=useState(null);
  const [servicePending,setServicePending]=useState(false);
  const [buildProfileDirty,setBuildProfileDirty]=useState(false),[buildProfilePending,setBuildProfilePending]=useState(false);
  const [workloadMenu, setWorkloadMenu] = useState(null);
  const [adoptionOpen,setAdoptionOpen]=useState(false), [adoptionDirty,setAdoptionDirty]=useState(false), [adoptionPending,setAdoptionPending]=useState(false), [adoptionEnvironment,setAdoptionEnvironment]=useState(environment);
  const [domainsDirty,setDomainsDirty]=useState({}),[domainsPending,setDomainsPending]=useState({});
  const pending = Object.values(domainsPending).some(Boolean) || settingsPending || resourcesPending || environmentsPending || adoptionPending || servicePending || buildProfilePending;
  useEffect(() => { setDirty(settingsDirty || resourcesDirty || environmentsDirty || adoptionDirty || buildProfileDirty || Object.values(domainsDirty).some(Boolean)); return () => setDirty(false); }, [settingsDirty, resourcesDirty, environmentsDirty, adoptionDirty, buildProfileDirty, domainsDirty, setDirty]);
  useEffect(() => { setOperationPending(pending); return () => setOperationPending(false); }, [pending, setOperationPending]);
  useEffect(() => {
    let active = true; setLoading(true); setWorkspaceError("");
    api("action", { action: "environment-workspace", project: id, environment }).then(result => {
      if (!active) return;
      if (result.environment?.name !== environment || result.project?.id !== id) throw new Error("The environment response does not match this workspace. Reload to try again.");
      setWorkspace(result);
    }).catch(error => active && setWorkspaceError(error.message || "Environment status is unavailable."))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [id, environment, api, reload, data.snapshots.containers?.updated]);
  if (!p) return <Empty title="Project not found"><Button onClick={() => navigate("projects")}>View projects</Button></Empty>;
  const current = workspace?.environment?.name === environment ? workspace : null;
  const containers = current?.services || [], runs = current?.runs || [], latest = runs[0], deployed = runs.find(run => run.status === "succeeded");
  const unassigned = p.has_legacy_workload && !p.legacy_binding;
  const existing = current?.legacy?.inventory_available === false && unassigned ? servicesFor(data,p) : current?.unassigned_services || [], legacyRuns = current?.unassigned_runs || [];
  const status = current?.environment?.state || "Status unavailable";
  const openBudgets = () => { setBudgetsViewed(true); setBudgetsOpen(true); };
  const containerList = containers.length ? <Services id={id} items={containers} environment={environment} accessBlockedReason={workspaceError ? "Environment membership is unavailable. Retry the environment status before opening a terminal. Container details remain available." : ""}/> : <Empty title={current?.environment?.state === "not-provisioned" ? "No containers provisioned" : "No verified containers"}>{current?.binding?.reason || "No containers are verified for this environment."}</Empty>;
  return <>
    <header className="project-heading">
      <div><div className="project-identity"><Folder sx={{fontSize:24,color:"text.secondary"}}/><Typography variant="h4" className="wrap-anywhere">{p.label || id}</Typography></div><div className="project-subtitle"><Link href={"https://github.com/"+p.repository} target="_blank" rel="noopener" color="text.secondary">{p.repository}</Link></div></div>
      <div className="project-heading-actions"><IconAction label="Reload environment" icon={Refresh} disabled={loading || pending} onClick={()=>setReload(value=>value+1)}/></div>
    </header>
    <div className="environment-workspace-bar">
      <Tabs value={environment} onChange={(_,value)=>onEnvironmentChange?.(value)} aria-label="Project environment" className="environment-switch"><Tab value="development" label="Development" disabled={pending}/><Tab value="production" label="Production" disabled={pending}/></Tabs>
      <div className="environment-workspace-state"><Typography fontSize={13} color="text.secondary">{loading && !current ? "Loading…" : status.replaceAll("-"," ").replace(/^./, letter => letter.toUpperCase())}</Typography><Hint label="About this environment">All workspace tabs apply to the selected environment. Existing containers are included only after their ownership is verified.</Hint></div>
    </div>
    {workspaceError && <Alert severity="error" sx={{mt:2}} action={<Button onClick={()=>setReload(value=>value+1)}>Retry</Button>}>{workspaceError}</Alert>}
    <Tabs value={tab} onChange={(_,value)=>{setTab(value); onTabChange?.(["overview", "containers", "deployments", "settings", "domains"][value]);}} className="project-tabs" variant="scrollable" scrollButtons="auto" allowScrollButtonsMobile aria-label={`${environmentLabel} workspace`}><Tab label="Overview"/><Tab label="Containers"/><Tab label="Deployments"/><Tab label="Settings"/><Tab label="Domains"/></Tabs>
    <Box sx={{display:tab===3?"block":"none"}}><EnvironmentSettings key={id} id={id} linkedProject={linkedProject} environment={environment} api={api} branches={Object.keys(branchState(data,id).branches || {})} BranchControl={BranchPicker} onDirty={setEnvironmentsDirty} onPending={setEnvironmentsPending} onSaved={()=>setReload(value=>value+1)}/><BuildProfile id={id} environment={environment} active={tab===3} api={api} onDirty={setBuildProfileDirty} onPending={setBuildProfilePending} onSaved={async()=>{setReload(value=>value+1);const result=await api('action',{action:'registry-get',project:id});setLinkedProject(result.project);await refresh();}}/><Box sx={{borderTop:1,borderColor:'divider',pt:2}}><Button color="inherit" onClick={openBudgets}>Project budgets</Button><Hint label="About project budgets">These budgets cover both environments. Saved allocations are policy settings, not proof of container ownership or applied runtime limits.</Hint></Box></Box>
    {['development','production'].map(target=><Box key={target} sx={{display:tab===1&&environment===target?'block':'none'}}><ServiceCreation project={id} environment={target} username={username} active={tab===1&&environment===target} api={api} navigate={navigate} onPending={setServicePending} onChanged={async()=>{await refresh();setReload(value=>value+1);}}/></Box>)}
    {['development','production'].map(target=><Box key={'domains:'+target} sx={{display:tab===4&&environment===target?'block':'none'}}><Domains key={id+':'+target} project={id} environment={target} username={username} active={tab===4&&environment===target} api={api} onDirty={value=>setDomainsDirty(previous=>previous[target]===value?previous:{...previous,[target]:value})} onPending={value=>setDomainsPending(previous=>previous[target]===value?previous:{...previous,[target]:value})} onContainers={()=>{setTab(1);onTabChange?.('containers');}} onIntegrations={()=>navigate('integrations')}/></Box>)}
    {tab!==3 && tab!==4 && loading && !current ? <Typography role="status" sx={{py:"var(--space-panel)"}}>Loading {environmentLabel.toLowerCase()} workspace…</Typography> : tab===0 ? <>
      <div className="release-band"><div><span>Last verified deployment</span><strong><code>{deployed?.sha?.slice(0,9)||"Not recorded"}</code><small>{deployed?date(deployed.updated):environmentLabel}</small></strong></div><div><span>Deployment branch</span><strong><Branch sx={{fontSize:14}}/>{current?.environment?.branch || "Unavailable"}<small>{current?.environment?.automatic?"Automatic preference on":"Automatic preference off"}</small></strong></div><div><span>Latest run</span><strong>{latest?<><Status value={latest.status}/><Button size="small" onClick={()=>navigate("job/"+latest.id)}>View run<Arrow sx={{fontSize:13,ml:.5}}/></Button></>:"No runs recorded"}</strong></div></div>
      <div className="section-heading"><Typography variant="h6">Containers <span className="count">{containers.length}</span></Typography><Button onClick={()=>{setTab(1);onTabChange?.("containers");}}>View containers</Button></div>{containerList}
      <Box sx={{mt:3,display:'flex',alignItems:'center',gap:1}}><Button variant="outlined" disabled={loading || pending || !!workspaceError || !(current?.capabilities?.build?.enabled || current?.capabilities?.deploy?.enabled)} onClick={()=>openBuild(id,null,{environment,branch:current.environment.branch,project_revision:current.project.revision,environment_revision:current.environment.revision,recipe_revision:current.environment.recipe_revision,build_source:current.build_source,deploy_enabled:!!current.capabilities?.deploy?.enabled})}>{`Build ${environmentLabel.toLowerCase()}`}</Button><Typography color="text.secondary" fontSize={13}>{current?.capabilities?.build?.reason || (!current?.capabilities?.build?.enabled ? "Environment deployment is not configured." : "")}</Typography></Box>
    </> : tab===1 ? <>{containerList}<ServiceVersions key={id+":"+environment} project={id} environment={environment} api={api} navigate={navigate} refreshKey={reload}/></> : tab===2 ? runs.length?<RunList jobs={runs}/>:<Empty title={`No ${environmentLabel.toLowerCase()} runs`}>Runs appear here when their recorded target belongs to this environment.</Empty> : null}
    {!!(unassigned || existing.length || legacyRuns.length) && <section className="unassigned-workload"><div><Typography variant="h6">Existing workload</Typography><Typography color="text.secondary" fontSize={13}>Environment unassigned <Hint label="About the existing workload">These containers and historical runs have not been verified as Development or Production. Their existing management remains available separately.</Hint></Typography></div><div className="unassigned-workload-actions">{unassigned&&<Button variant="outlined" disabled={pending} onClick={()=>{if(!adoptionDirty)setAdoptionEnvironment(environment);setAdoptionOpen(true);}}>{adoptionDirty?'Check linking request':'Link to '+environmentLabel}</Button>}<Button variant="outlined" onClick={()=>setLegacyView("containers")}>Containers{existing.length?` (${existing.length})`:""}</Button><Button color="inherit" aria-haspopup="menu" aria-controls={workloadMenu ? "workload-actions" : undefined} aria-expanded={!!workloadMenu} onClick={event=>setWorkloadMenu(event.currentTarget)}>More actions</Button><ActionMenu id="workload-actions" anchorEl={workloadMenu} open={!!workloadMenu} onClose={()=>setWorkloadMenu(null)}><MenuItem onClick={()=>{setWorkloadMenu(null);setLegacyView("history");}}><ListItemIcon><History fontSize="small"/></ListItemIcon>Deployment history</MenuItem><MenuItem onClick={()=>{setWorkloadMenu(null);setLegacyView("settings");}}><ListItemIcon><Settings fontSize="small"/></ListItemIcon>Deployment settings</MenuItem>{p.build_ready&&<MenuItem disabled={pending} onClick={()=>{setWorkloadMenu(null);openBuild(id,null,{environment:"legacy-unclassified"});}}><ListItemIcon><PlayArrow fontSize="small"/></ListItemIcon>Build existing workload</MenuItem>}</ActionMenu></div></section>}
    <AdoptWorkload open={adoptionOpen} project={id} environment={adoptionEnvironment} api={api} onDirty={setAdoptionDirty} onPending={setAdoptionPending} onClose={()=>setAdoptionOpen(false)} onLinked={async(project)=>{setLinkedProject(project);await refresh();setReload(value=>value+1);setAdoptionOpen(false);}}/>
    <Dialog open={!!legacyView} onClose={()=>!settingsPending&&setLegacyView(null)} keepMounted fullWidth maxWidth="lg" aria-labelledby="legacy-workload-title"><DialogTitle id="legacy-workload-title">Existing workload · Environment unassigned</DialogTitle><DialogContent dividers><Box sx={{display:legacyView==="containers"?"block":"none"}}>{existing.length?<Services id={id} items={existing}/>:<Empty title="Container inventory unavailable">{workspaceError || "Reload the workspace to inspect the unassigned container inventory."}<Button onClick={()=>setReload(value=>value+1)}>Reload</Button></Empty>}</Box><Box sx={{display:legacyView==="history"?"block":"none"}}>{legacyRuns.length?<RunList jobs={legacyRuns}/>:<Empty title="No unassigned runs recorded"/>}</Box><Box sx={{display:legacyView==="settings"?"block":"none"}}><ProjectSettings id={id} setDirty={setSettingsDirty} setOperationPending={setSettingsPending} onEnvironments={()=>{setLegacyView(null);setTab(3);}}/></Box></DialogContent><DialogActions><Button disabled={settingsPending} onClick={()=>setLegacyView(null)}>Close</Button></DialogActions></Dialog>
    <Dialog open={budgetsOpen} onClose={()=>!resourcesPending&&setBudgetsOpen(false)} keepMounted fullWidth maxWidth="md" aria-labelledby="project-budgets-title"><DialogTitle id="project-budgets-title">Project budgets · Both environments</DialogTitle><DialogContent dividers>{budgetsViewed&&<Resources key={id} id={id} services={servicesFor(data,p)} verifiedServices={workspaceError ? [] : containers} verifiedEnvironment={environment} footerContainer={resourceFooter} navigate={route=>{setBudgetsOpen(false);navigate(route);}} api={api} setDirty={setResourcesDirty} setOperationPending={setResourcesPending}/>}</DialogContent><DialogActions><Box component="span" ref={setResourceFooter} sx={{display:"contents"}}/><Button disabled={resourcesPending} onClick={()=>setBudgetsOpen(false)}>Close</Button></DialogActions></Dialog>
  </>;
}
function ProjectSettings({ id, setDirty, setOperationPending, onEnvironments }) {
  const { data, action, notify } = useApp(),
    p = data.projects[id];
  const [draft, setDraft] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    setOperationPending(busy);
    return () => setOperationPending(false);
  }, [busy, setOperationPending]);
  const values = draft || { branch: p.branch, automatic: !!p.automatic },
    dirty =
      !!draft &&
      (draft.branch !== p.branch || draft.automatic !== !!p.automatic),
    b = branchState(data, id);
  useEffect(() => {
    setDirty(dirty);
    return () => setDirty(false);
  }, [dirty, setDirty]);
  if (p.has_legacy_workload === false)
    return <Box sx={{py:3}}><Typography sx={{mb:2}}>Choose an environment to configure deployment.</Typography><Button onClick={onEnvironments}>Environment settings</Button></Box>;
  return (
    <Box sx={{ maxWidth: 680 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{mb:2}}>
        <Typography variant="h6">Deployment settings</Typography>
        <Hint label="About deployment settings">Pushes to the tracked branch build and deploy when enabled. Manual builds can use another branch. Saving settings does not start a deployment.</Hint>
      </Stack>
      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}
      {(!b.fresh || b.error) && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Branch observations need refreshing. {b.error || ""}
        </Alert>
      )}
      <BranchPicker
        disabled={busy}
        options={Object.keys(b.branches).sort()}
        value={values.branch || null}
        onChange={(event, branch) =>
          setDraft({ ...values, branch: branch || "" })
        }
        label="Tracked branch"
        error={!!values.branch && !b.branches[values.branch]}
        helperText={
          values.branch && !b.branches[values.branch]
            ? "This branch is not in the latest observation."
            : undefined
        }
        onRefresh={() =>
          action({ action: "refresh-branches", project: id }).catch((error) =>
            setError(error.message),
          )
        }
      />
      <FormControlLabel
        sx={{ my: 3 }}
        control={
          <Switch
            disabled={busy}
            checked={values.automatic}
            onChange={(e) =>
              setDraft({ ...values, automatic: e.target.checked })
            }
          />
        }
        label="Build and deploy automatically"
      />
      <Divider />
      <Stack
        direction="row"
        spacing={1}
        useFlexGap
        alignItems="center"
        sx={{ mt: 3, flexWrap: "wrap" }}
      >
        <Button
          variant="contained"
          disabled={busy || !dirty || !b.fresh || !b.branches[values.branch]}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              await action({ action: "settings", project: id, ...values });
              setDraft(null);
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Saving…" : "Save changes"}
        </Button>
        <Button
          disabled={!dirty || busy}
          color="inherit"
          onClick={() => setDraft(null)}
        >
          Discard changes
        </Button>
        <Typography color="text.secondary" fontSize={12}>
          {dirty ? "Unsaved changes" : "All changes saved"}
        </Typography>
      </Stack>

    </Box>
  );
}
function ProjectsPage() {
  const { data, navigate, api, refresh, creationDraft, saveCreationDraft, setOperationPending } = useApp();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [registry, setRegistry] = useState(null), [registryError, setRegistryError] = useState(''), [registryReload, setRegistryReload] = useState(0), [registryLoading, setRegistryLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setRegistryError('');
    setRegistryLoading(true);
    api('action', {action:'registry-list'}).then(result => {
      if (!result.projects || typeof result.projects !== 'object' || Array.isArray(result.projects)) throw Error('Environment settings are unavailable.');
      if (active) setRegistry(result.projects);
    }).catch(error => active && setRegistryError(error.message || 'Environment settings could not be loaded.')).finally(()=>active && setRegistryLoading(false));
    return () => { active = false; };
  }, [api, registryReload]);
  const entries = Object.entries(data.projects).filter(([id, p]) =>
    (id + " " + p.label + " " + p.repository)
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <>
      <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={2}>
        <Typography variant="h4">Projects</Typography>
        <Stack direction="row" alignItems="center" spacing={1}><IconAction label="Refresh project settings" icon={Refresh} disabled={registryLoading} onClick={()=>setRegistryReload(value=>value+1)}/><Button variant="contained" onClick={() => setCreating(true)}>New project</Button></Stack>
      </Stack>
      <ProjectCreateDialog open={creating} onClose={() => setCreating(false)} api={api} draftStore={creationDraft} onDraftChange={saveCreationDraft} onPending={setOperationPending} onIntegrations={()=>navigate("integrations")} onCreated={async (projectId) => { await refresh(); navigate("project/" + encodeURIComponent(projectId)); }} />
      <Typography color="text.secondary" sx={{ mt: 1 }}>
        {Object.keys(data.projects).length} projects
      </Typography>
      {registryError && <Alert severity="error" sx={{mt:2}} action={<Button color="inherit" onClick={()=>setRegistryReload(value=>value+1)}>Retry</Button>}>{registryError}</Alert>}
      <TextField
        fullWidth
        size="small"
        label="Search projects"
        sx={{ my: 3 }}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        slotProps={{
          input: {
            startAdornment: (
              <InputAdornment position="start">
                <Search fontSize="small" />
              </InputAdornment>
            ),
          },
        }}
      />
      {entries.length ? (
        entries.map(([id, p]) => {
          const environments = registry?.[id]?.environments;
          return (
            <Tooltip key={id} describeChild enterTouchDelay={0} leaveTouchDelay={10000} title="Environment states are saved settings. Auto is the automatic-deploy preference; it does not indicate running containers.">
            <button
              className="directory-row"
              onClick={() => navigate("project/" + encodeURIComponent(id))}
            >
              <Folder sx={{ fontSize: 20, color: "text.secondary" }} />
              <strong>
                {p.label || id}
                <small>{p.repository}</small>
                {p.has_legacy_workload && !p.legacy_binding && <small>Existing workload unassigned</small>}
              </strong>
              <span className="directory-environments">
                {['development', 'production'].map(environment => {
                  const saved = environments?.[environment];
                  return <span key={environment}><b>{environment === 'production' ? 'Production' : 'Development'}</b><small>{registryError ? 'Settings unavailable' : !registry ? 'Loading settings…' : saved ? `${typeof saved.state === 'string' ? saved.state.replaceAll('-', ' ').replace(/^./, letter=>letter.toUpperCase()) : 'State unavailable'} · Auto: ${saved.automatic ? 'on' : 'off'}` : 'Settings unavailable'}</small></span>;
                })}
              </span>
              <Chevron sx={{ fontSize: 18 }} />
            </button>
            </Tooltip>
          );
        })
      ) : (
        <Empty title="No matching projects">
          Try a project name or repository.
        </Empty>
      )}
    </>
  );
}
function RunList({ jobs }) {
  const { data, navigate } = useApp();
  if (!jobs.length)
    return (
      <Empty title="No runs found">
        Builds matching this selection will appear here.
      </Empty>
    );
  return (
    <div className="runs-list">
      {jobs.map((j) => (
        <button
          className="deployment-row"
          key={j.id}
          onClick={() => navigate("job/" + j.id)}
        >
          <Status value={j.status} />
          <span>
            <strong>{label(data, j.project)}</strong>
            <small>
              #{j.id} · {j.branch || "Branch not recorded"} ·{" "}
              {j.sha?.slice(0, 9)}
            </small>
          </span>
          <span>{date(j.updated)}</span>
          <Chevron sx={{ fontSize: 18 }} />
        </button>
      ))}
    </div>
  );
}
function RunsPage() {
  const { data } = useApp();
  const [project, setProject] = useState(null),
    [status, setStatus] = useState(null);
  const options = [...new Set(data.jobs.map((j) => j.status))];
  return (
    <>
      <Typography variant="h4" sx={{ mb: 3 }}>
        Deployments
      </Typography>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 3 }}>
        <Autocomplete
          fullWidth
          options={Object.keys(data.projects)}
          getOptionLabel={(id) => label(data, id)}
          value={project}
          onChange={(e, v) => setProject(v)}
          renderInput={(p) => <TextField {...p} label="Project" />}
        />
        <Autocomplete
          fullWidth
          options={options}
          getOptionLabel={statusLabel}
          value={status}
          onChange={(e, v) => setStatus(v)}
          renderInput={(p) => <TextField {...p} label="Status" />}
        />
      </Stack>
      <RunList
        jobs={data.jobs.filter(
          (j) =>
            (!project || j.project === project) &&
            (!status || j.status === status),
        )}
      />
      <Typography color="text.secondary" fontSize={12} sx={{ mt: 2 }}>
        Latest 100 runs · retained for 30 days
      </Typography>
    </>
  );
}
function RunPage({ id }) {
  const { data, navigate, openBuild, action } = useApp();
  const job = data.jobs.find((job) => job.id === Number(id));
  return (
    <RunDetail
      key={id}
      job={job}
      project={data.projects[job?.project]}
      events={data.events}
      onNavigate={navigate}
      onBuild={openBuild}
      action={action}
    />
  );
}
function BuildDialog({ context, onClose }) {
  const { data, api, refresh, notify, navigate } = useApp();
  const p = data.projects[context.project], retry = context.retry;
  const environment = context.target?.environment || retry?.target?.environment || null;
  const scoped = environment === 'development' || environment === 'production';
  const legacy = environment === 'legacy-unclassified';
  const environmentLabel = scoped ? environment === 'production' ? 'Production' : 'Development' : legacy ? 'Existing workload · Environment unassigned' : 'Target not recorded';
  const [branch,setBranch]=useState(retry?.branch || context.target?.branch || (legacy?p.branch:''));
  const [mode,setMode]=useState(retry?.mode==='validate'&&retry?.status!=='validated'?'validate':context.target?.build_source?.source_revision&&!context.target?.deploy_enabled?'validate':'deploy');
  const [selection,setSelection]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[requestId,setRequestId]=useState(()=>crypto.randomUUID()),[submitted,setSubmitted]=useState(null),[target,setTarget]=useState(null),[targetLoading,setTargetLoading]=useState(scoped),[targetError,setTargetError]=useState(''),[targetReload,setTargetReload]=useState(0);
  const requestLock=useRef(false);
  const locked=busy||!!submitted;
  const b=branchState(data,context.project);
  useEffect(()=>{
    if(!scoped)return;
    let active=true;setTargetLoading(true);setTargetError('');setTarget(null);
    api('action',{action:'environment-workspace',project:context.project,environment}).then(result=>{
      if(!active)return;
      if(result.project?.id!==context.project||result.environment?.name!==environment)throw Error('The inspected environment does not match this build target.');
      setTarget(result);setBranch(value=>value || result.environment.branch);
      if(!result.capabilities?.deploy?.enabled&&result.capabilities?.build?.enabled)setMode('validate');
    }).catch(error=>active&&setTargetError(error.message)).finally(()=>active&&setTargetLoading(false));
    return()=>{active=false;};
  },[context.project,environment,api,targetReload]);
  useEffect(()=>{if(!b.fresh)api('action',{action:'refresh-branches',project:context.project}).catch(error=>setError(error.message));},[context.project]);
  useEffect(()=>{if(!selection&&b.fresh&&b.branches[branch])setSelection({branch,sha:b.branches[branch]});},[b.fresh,b.updated,branch,selection]);
  const changed=selection&&b.branches[selection.branch]!==selection.sha;
  const buildAllowed=legacy?!!p.build_ready:!!target?.capabilities?.build?.enabled;
  const deployAllowed=legacy?!!p.build_ready:!!target?.capabilities?.deploy?.enabled;
  const allowed=mode==='deploy'?deployAllowed:buildAllowed;
  const managedDeploy=target?.capabilities?.deploy?.kind==='managed-build';
  const useBuildSource=!!target?.build_source?.source_revision&&(mode==='validate'||managedDeploy);
  const managedServiceCount=target?.services?.filter(service=>service.State==='running').length || 0;
  const stoppedServiceCount=(target?.services?.length || 0)-managedServiceCount;
  const disabledReason=!environment?'This historical run has no verified target. Open its project and choose an environment for a new build.':scoped?(mode==='deploy'?target?.capabilities?.deploy?.reason:target?.capabilities?.build?.reason):!p.build_ready?'Build definitions are not configured.':'';
  function choose(value){setBranch(value||'');setSelection(value?{branch:value,sha:b.branches[value]}:null);setRequestId(crypto.randomUUID());}
  return <Dialog open fullWidth maxWidth="sm" onClose={()=>!busy&&onClose()} aria-labelledby="build-dialog-title">
    <DialogTitle id="build-dialog-title">{retry?'Retry run #'+retry.id:'New build'}<Typography color="text.secondary" fontSize={13} sx={{mt:1}}>{p.label||context.project} · {environmentLabel}</Typography></DialogTitle>
    <DialogContent dividers><Stack spacing={2}>
      {error&&<Alert severity="error">{error}{submitted&&' Checking again keeps the original request and target.'}</Alert>}
      {targetError&&<Alert severity="error">{targetError}</Alert>}
      {targetLoading&&<Typography role="status">Inspecting {environment} target…</Typography>}
      {!targetLoading&&!allowed&&disabledReason&&<Typography color="text.secondary">{disabledReason}</Typography>}
      {!b.fresh&&<Typography color="text.secondary">Waiting for current branch observations.</Typography>}
      <BranchPicker disabled={locked||targetLoading||!environment} options={Object.keys(b.branches).sort()} value={branch||null} onChange={(_,value)=>choose(value)} label="Build branch" error={!!branch&&!b.branches[branch]} helperText={branch&&!b.branches[branch]?'Choose an available branch.':undefined} onRefresh={()=>api('action',{action:'refresh-branches',project:context.project}).catch(error=>setError(error.message))}/>
      <Box><Typography color="text.secondary" fontSize={12}>Commit to build</Typography><Typography component="code" sx={{overflowWrap:'anywhere',fontSize:12}}>{selection?.sha||'Select a branch'}</Typography></Box>
      {scoped&&target&&<Typography color="text.secondary" fontSize={13}>Environment revision {target.environment.revision} · {useBuildSource?'Source revision '+target.build_source.source_revision:'Recipe '+(target.environment.recipe_revision || 'not configured')}<Hint label="About the build target">This request pins the reviewed environment and build configuration. Later settings changes require a new review.</Hint></Typography>}
      {scoped&&target&&!targetLoading&&((managedDeploy&&deployAllowed&&mode==='deploy')||(!deployAllowed&&allowed))&&<Typography color="text.secondary" fontSize={13}>{managedDeploy&&deployAllowed?`Deployment updates ${managedServiceCount} running managed ${managedServiceCount===1?'service':'services'} in ${environmentLabel}.${stoppedServiceCount?` ${stoppedServiceCount} stopped ${stoppedServiceCount===1?'service stays':'services stay'} unchanged.`:''}`:target.capabilities?.deploy?.reason}</Typography>}
      {changed&&<Alert severity="warning">The branch changed. Review the latest commit.</Alert>}
      {retry&&<Typography color="text.secondary" fontSize={13}>Uses the latest branch commit and the reviewed configuration for the original environment.</Typography>}
      <RadioGroup value={mode} onChange={event=>{setMode(event.target.value);setRequestId(crypto.randomUUID());}}><FormControlLabel value="deploy" control={<Radio disabled={locked||targetLoading||!deployAllowed}/>} label={scoped?`Build, test & deploy to ${environmentLabel}`:"Build, test & deploy"}/><FormControlLabel value="validate" control={<Radio disabled={locked||targetLoading||!buildAllowed}/>} label="Build & test only"/></RadioGroup>
    </Stack></DialogContent>
    <DialogActions>
      {!environment&&<Button onClick={()=>{onClose();navigate('project/'+encodeURIComponent(context.project));}}>Open project</Button>}
      {scoped&&<Button disabled={locked||targetLoading} onClick={()=>{setTargetReload(value=>value+1);setRequestId(crypto.randomUUID());}}>Refresh target</Button>}
      {changed&&<Button disabled={locked} onClick={()=>choose(branch)}>Use latest commit</Button>}
      <Button disabled={busy} onClick={onClose}>Cancel</Button>
      <Button variant="contained" disabled={busy||(!submitted&&(!allowed||targetLoading||!!targetError||!selection?.sha||!b.fresh||!!changed||!b.branches[branch]))} onClick={async()=>{
        if(requestLock.current)return;requestLock.current=true;setBusy(true);setError('');
        try {
          const reviewedTarget=scoped?{environment,project_revision:target.project.revision,environment_revision:target.environment.revision,...(useBuildSource?{source_revision:target.build_source.source_revision}:{recipe_revision:target.environment.recipe_revision})}:{environment};
          const payload=submitted||buildPayload({project:context.project,branch,sha:selection.sha,mode,retry,requestId,target:reviewedTarget});
          setSubmitted(payload);const result=await api('action',payload);notify(result.message);onClose();navigate('job/'+result.job_id);refresh().catch(()=>{});
        }catch(error){setError(error.message);if(error.status&&error.status<500)setSubmitted(null);}
        finally{requestLock.current=false;setBusy(false);}
      }}>{busy?'Queuing…':submitted?'Check original request':mode==='deploy'?'Queue deployment':'Queue build & tests'}</Button>
    </DialogActions>
  </Dialog>;
}
function ServicesPage() {
  const { data } = useApp(),
    [query, setQuery] = useState("");
  const owned = new Set(
      Object.values(data.projects).flatMap((p) =>
        servicesFor(data, p).map((c) => c.Names),
      ),
    ),
    shared = (data.snapshots.containers?.value || []).filter(
      (c) => !owned.has(c.Names),
    );
  return (
    <>
      <Typography variant="h4">Containers</Typography>
      <TextField
        label="Find a project, service or image"
        fullWidth
        sx={{ my: 3 }}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {Object.entries(data.projects).map(([id, p]) => {
        const all = servicesFor(data, p),
          q = query.toLowerCase(),
          items = all.filter((c) =>
            (p.label + " " + id + " " + c.Names + " " + c.Image)
              .toLowerCase()
              .includes(q),
          );
        return items.length ? (
          <Box key={id} sx={{ mb: "var(--space-section)" }}>
            <Typography variant="h6" sx={{ mb: 2 }}>
              {p.label || id}
            </Typography>
            <Services id={id} items={items} />
          </Box>
        ) : null;
      })}
      {shared.filter((c) =>
        (c.Names + " " + c.Image).toLowerCase().includes(query.toLowerCase()),
      ).length > 0 && (
        <>
          <Typography variant="h6" sx={{ my: 2 }}>
            Shared infrastructure
          </Typography>
          <Services
            items={shared.filter((c) =>
              (c.Names + " " + c.Image)
                .toLowerCase()
                .includes(query.toLowerCase()),
            )}
          />
        </>
      )}
    </>
  );
}
function Activity() {
  const { data } = useApp();
  return (
    <>
      <Typography variant="h4" sx={{ mb: 3 }}>
        Activity
      </Typography>
      {!data.audit.length && !data.control_runs?.length ? (
        <Empty title="No activity recorded">
          Sign-ins and management actions will appear here.
        </Empty>
      ) : (
        <>
          {data.control_runs?.length > 0 && (
            <>
              <Typography variant="h6">Container operations</Typography>
              {data.control_runs.map((r) => (
                <div className="event-row" key={"c" + r.id}>
                  <time>{date(r.created)}</time>
                  <div>
                    <strong>
                      {r.project} / {r.container}
                    </strong>
                    <Typography>
                      {r.operation} · {r.status}
                    </Typography>
                    <pre>{r.detail}</pre>
                  </div>
                </div>
              ))}
            </>
          )}
          <Typography variant="h6" sx={{ my: 3 }}>
            Operator history
          </Typography>
          {data.audit.map((a) => (
            <div className="event-row" key={a.id}>
              <time>{date(a.created)}</time>
              <div>
                <strong>
                  {a.actor} · {a.action}
                </strong>
                <Typography>{a.project}</Typography>
                <pre>{a.detail}</pre>
              </div>
            </div>
          ))}
        </>
      )}
      <Typography fontSize={12} color="text.secondary" sx={{ mt: 3 }}>
        Administrative activity retained for 90 days.
      </Typography>
    </>
  );
}
function Overview() {
  const { data, connection, navigate } = useApp();
  const active = data.jobs.filter((j) => activeStates.includes(j.status)),
    issues = data.jobs.filter((j) =>
      [
        "failed",
        "interrupted",
        "rolled-back",
        "needs-review",
        "awaiting-validation",
        "auto-deploy-disabled",
      ].includes(j.status),
    );
  return (
    <>
      <Typography variant="h4">Overview</Typography>
      <div className="fleet-strip">
        <Button color="inherit" onClick={() => navigate("projects")}>
          {Object.keys(data.projects).length} projects
        </Button>
        <Button color="inherit" onClick={() => navigate("containers")}>
          {(data.snapshots.containers?.value || []).length} containers observed
        </Button>
        <span>{active.length} active / queued runs</span>
      </div>
      {active.length > 0 && (
        <Box sx={{ mb: "var(--space-section)" }}>
          <Typography variant="h6">Active deployments</Typography>
          <RunList jobs={active} />
        </Box>
      )}
      {issues.length > 0 && (
        <Box sx={{ mb: "var(--space-section)" }}>
          <Typography variant="h6">Needs attention</Typography>
          <RunList jobs={issues} />
        </Box>
      )}
      <Telemetry data={data} connection={connection} />
      <Typography variant="caption" color="text.secondary">
        External uptime checks and outbound alerts are not configured.
      </Typography>
    </>
  );
}
createRoot(document.getElementById("root")).render(<App />);
