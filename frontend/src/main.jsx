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
  RadioGroup,
  Radio,
  useMediaQuery,
} from "@mui/material";
import ContainerPage from "./ContainerPage.jsx";
import RunDetail from "./RunDetail.jsx";
import Telemetry from "./Telemetry.jsx";
import {
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
    <Box sx={{ py: 5, textAlign: "center" }}>
      <Typography variant="h6">{title}</Typography>
      <Typography color="text.secondary" sx={{ mt: 1 }}>
        {children}
      </Typography>
    </Box>
  );
}
function App() {
  const [session, setSession] = useState(null),
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
      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: "none",
            fontSize: 13,
            minWidth: 0,
            padding: "16px 0",
            marginRight: 26,
          },
        },
      },
      MuiTooltip: { defaultProps: { enterTouchDelay: 0 } },
      MuiAlert: {
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
            padding: "20px 24px",
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
            padding: "24px",
            "&.MuiDialogContent-root": { paddingTop: 24 },
            overflowY: "auto",
          },
        },
      },
      MuiDialogActions: {
        styleOverrides: {
          root: ({ theme }) => ({
            padding: "16px 24px",
            borderTop: `1px solid ${theme.palette.divider}`,
            flexShrink: 0,
            flexWrap: "wrap",
            gap: 8,
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
    data,
    api,
    refresh,
    notify: setToast,
    navigate,
    openBuild: (project, retry = null) => setBuild({ project, retry }),
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
                        navigate("project/" + encodeURIComponent(id))
                      }
                    >
                      {label(data, id)}
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
                              containers: "Services",
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
                  <ProjectPage key={id} id={id} />
                ) : page === "container" ? (
                  <ContainerPage
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
                  />
                ) : page === "jobs" ? (
                  <RunsPage />
                ) : page === "job" ? (
                  <RunPage id={id} />
                ) : page === "projects" ? (
                  <ProjectsPage />
                ) : page === "containers" ? (
                  <ServicesPage />
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
          ["containers", "Services", Dns],
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
              Your projects, deployments and services.
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
function Services({ id, items }) {
  const { navigate, observationFailed, data } = useApp();
  const old = stale(data, observationFailed);
  if (!items.length)
    return (
      <Empty title="No services observed">
        Check the project mapping or wait for the collector.
      </Empty>
    );
  return (
    <div className="service-table">
      <div className="table-head">
        <span>Service / image</span>
        <span>State / health</span>
        <span>CPU / memory</span>
        <span>Access</span>
      </div>
      {items.map((c) => (
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
                    "container/" +
                      encodeURIComponent(id) +
                      "/" +
                      encodeURIComponent(c.Names),
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
                  onClick={() =>
                    navigate(
                      "container/" +
                        encodeURIComponent(id) +
                        "/" +
                        encodeURIComponent(c.Names) +
                        "?tab=terminal",
                    )
                  }
                />
                <IconAction
                  label={"Manage · " + c.Names}
                  icon={Settings}
                  onClick={() =>
                    navigate(
                      "container/" +
                        encodeURIComponent(id) +
                        "/" +
                        encodeURIComponent(c.Names),
                    )
                  }
                />
              </>
            ) : (
              <Typography variant="caption">Observed only</Typography>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
function ProjectPage({ id }) {
  const { data, navigate, openBuild, observationFailed } = useApp();
  const [tab, setTab] = useState(0),
    p = data.projects[id];
  if (!p)
    return (
      <Empty title="Project not found">
        <Button onClick={() => navigate("projects")}>View projects</Button>
      </Empty>
    );
  const services = servicesFor(data, p),
    runs = data.jobs.filter((j) => j.project === id),
    latest = runs[0],
    deployed = runs.find((j) => j.status === "succeeded"),
    old = stale(data, observationFailed);
  return (
    <>
      <header className="project-heading">
        <div>
          <div className="project-identity">
            <Folder sx={{ fontSize: 24, color: "text.secondary" }} />
            <Typography variant="h4" className="wrap-anywhere">
              {p.label || id}
            </Typography>
          </div>
          <div className="project-subtitle">
            <Link
              href={"https://github.com/" + p.repository}
              target="_blank"
              rel="noopener"
              color="text.secondary"
            >
              {p.repository}
            </Link>
            <span>Production</span>
          </div>
        </div>
        <div className="project-heading-actions">
          <IconAction
            label="Project settings"
            icon={Settings}
            onClick={() => setTab(3)}
          />
          <Button
            variant="contained"
            startIcon={<Rocket />}
            disabled={!p.build_ready}
            onClick={() => openBuild(id)}
          >
            New build
          </Button>
        </div>
      </header>
      <div className="release-band">
        <div>
          <span>Last verified deployment</span>
          <strong>
            <code>{deployed?.sha?.slice(0, 9) || "Not recorded"}</code>
            <small>
              {deployed
                ? date(deployed.updated)
                : "Inspect service image versions below"}
            </small>
          </strong>
        </div>
        <div>
          <span>Automatic branch</span>
          <strong>
            <Branch sx={{ fontSize: 14 }} />
            {p.branch}
            <small>{p.automatic ? "Enabled" : "Disabled"}</small>
          </strong>
        </div>
        <div>
          <span>Latest run</span>
          <strong>
            {latest ? (
              <>
                <Status value={latest.status} />
                <Button
                  size="small"
                  onClick={() => navigate("job/" + latest.id)}
                >
                  View run
                  <Arrow sx={{ fontSize: 13, ml: 0.5 }} />
                </Button>
              </>
            ) : (
              "No runs recorded"
            )}
          </strong>
        </div>
      </div>
      {!p.build_ready && (
        <Alert severity="warning" sx={{ mt: 2 }}>
          Build definitions and tests are not configured. Container management
          remains available.
        </Alert>
      )}
      <Tabs value={tab} onChange={(e, v) => setTab(v)} className="project-tabs">
        <Tab label="Overview" />
        <Tab label="Services" />
        <Tab label="Deployments" />
        <Tab label="Settings" />
      </Tabs>
      <Box sx={{ display: tab === 3 ? "block" : "none" }}>
        <ProjectSettings key={id} id={id} />
      </Box>
      {tab === 3 ? null : tab === 2 ? (
        <RunList jobs={runs} />
      ) : tab === 1 ? (
        <Services id={id} items={services} />
      ) : (
        <div className="project-layout">
          <div className="project-primary">
            <div className="project-status">
              <div>
                <Status
                  value={
                    old
                      ? "Stale"
                      : services.some((c) => c.Status?.includes("unhealthy"))
                        ? "Unhealthy"
                        : services.length &&
                            services.every((c) => c.State === "running")
                          ? "Running"
                          : "Needs attention"
                  }
                />
                <span>
                  {services.filter((c) => c.State === "running").length} of{" "}
                  {services.length} services running
                </span>
              </div>
            </div>
            <div className="section-heading">
              <Typography variant="h6">
                Services <span className="count">{services.length}</span>
              </Typography>
            </div>
            <Services id={id} items={services} />
          </div>
          <aside className="deployment-aside">
            <Typography variant="h6">Deployment activity</Typography>
            {latest ? (
              <>
                <div className="deploy-success">
                  <Status value={latest.status} />
                </div>
                <div className="commit-message">
                  {latest.detail ||
                    "Open the run to inspect its execution record."}
                </div>
                <div className="commit-ref">
                  <Branch sx={{ fontSize: 14 }} />
                  {latest.branch}
                  <span>{latest.sha?.slice(0, 9)}</span>
                </div>
                <div className="deploy-facts">
                  <span>Updated</span>
                  <strong>{date(latest.updated)}</strong>
                  <span>Triggered by</span>
                  <strong>{latest.actor || "GitHub"}</strong>
                  <span>Mode</span>
                  <strong>
                    {latest.mode === "validate"
                      ? "Build & tests"
                      : "Deployment"}
                  </strong>
                </div>
                <Button
                  fullWidth
                  variant="outlined"
                  color="inherit"
                  endIcon={<Arrow />}
                  onClick={() => navigate("job/" + latest.id)}
                >
                  View run #{latest.id}
                </Button>
              </>
            ) : (
              <Typography color="text.secondary" sx={{ mt: 2 }}>
                No deployment history yet.
              </Typography>
            )}
            <div className="aside-divider" />
            <div className="section-heading">
              <Typography variant="h6">Delivery</Typography>
              <IconAction
                label="Edit deployment settings"
                icon={Settings}
                onClick={() => setTab(3)}
              />
            </div>
            <div className="delivery-line">
              <span>Automatic deploy</span>
              <strong>{p.automatic ? "On" : "Off"}</strong>
            </div>
            <div className="delivery-line">
              <span>Tracked branch</span>
              <code>{p.branch}</code>
            </div>
            <Typography fontSize={12} color="text.secondary" sx={{ mt: 2 }}>
              A recorded deployment is separate from each container’s currently
              observed image.
            </Typography>
          </aside>
        </div>
      )}
    </>
  );
}
function ProjectSettings({ id }) {
  const { data, action, notify, setDirty, setOperationPending } = useApp(),
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
  return (
    <Box sx={{ maxWidth: 680 }}>
      <Typography variant="h6">Deployment settings</Typography>
      <Typography color="text.secondary" sx={{ mt: 1, mb: 3 }}>
        Pushes to this branch build, test and deploy automatically when enabled.
        Manual builds may use a different branch.
      </Typography>
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
        spacing={2}
        alignItems="center"
        sx={{ mt: 3, flexWrap: "wrap", gap: 1 }}
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
      <Typography fontSize={12} color="text.secondary" sx={{ mt: 2 }}>
        Saving these settings does not start a deployment.
      </Typography>
    </Box>
  );
}
function ProjectsPage() {
  const { data, navigate } = useApp();
  const [search, setSearch] = useState("");
  const entries = Object.entries(data.projects).filter(([id, p]) =>
    (id + " " + p.label + " " + p.repository)
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <>
      <Typography variant="h4">Projects</Typography>
      <Typography color="text.secondary" sx={{ mt: 1 }}>
        {Object.keys(data.projects).length} projects ·{" "}
        {Object.values(data.projects).filter((p) => p.automatic).length} with
        automatic deployment on
      </Typography>
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
          const services = servicesFor(data, p),
            j = data.jobs.find((j) => j.project === id);
          return (
            <button
              className="directory-row"
              key={id}
              onClick={() => navigate("project/" + encodeURIComponent(id))}
            >
              <Folder sx={{ fontSize: 20, color: "text.secondary" }} />
              <strong>
                {p.label || id}
                <small>{p.repository}</small>
              </strong>
              <span>{p.branch}</span>
              <span>{services.length} services</span>
              {j && <Status value={j.status} />}
              <Chevron sx={{ fontSize: 18 }} />
            </button>
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
  const { data, api, refresh, notify, navigate } = useApp(),
    p = data.projects[context.project],
    retry = context.retry;
  const [branch, setBranch] = useState(retry?.branch || p.branch),
    [mode, setMode] = useState(
      retry?.mode === "validate" && retry?.status !== "validated"
        ? "validate"
        : "deploy",
    ),
    [selection, setSelection] = useState(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [requestId, setRequestId] = useState(() => crypto.randomUUID()),
    [submitted, setSubmitted] = useState(null);
  const locked = busy || !!submitted;
  const b = branchState(data, context.project);
  useEffect(() => {
    if (!b.fresh)
      api("action", {
        action: "refresh-branches",
        project: context.project,
      }).catch((e) => setError(e.message));
  }, [context.project]);
  useEffect(() => {
    if (!selection && b.fresh && b.branches[branch])
      setSelection({ branch, sha: b.branches[branch] });
  }, [b.fresh, b.updated, branch, selection]);
  const changed = selection && b.branches[selection.branch] !== selection.sha;
  function choose(value) {
    setBranch(value || "");
    setSelection(value ? { branch: value, sha: b.branches[value] } : null);
    setRequestId(crypto.randomUUID());
  }
  return (
    <Dialog open fullWidth maxWidth="sm" onClose={() => !busy && onClose()}>
      <DialogTitle>
        {retry ? "Retry run #" + retry.id : "New build"}
        <Typography color="text.secondary" fontSize={13} sx={{ mt: 1 }}>
          {p.label || context.project}
        </Typography>
      </DialogTitle>
      <DialogContent>
        <Stack spacing={3} sx={{ pt: 1 }}>
          {error && (
            <Alert severity="error">
              {error}
              {submitted &&
                " Retry checks the original request; it will not create a duplicate run."}
            </Alert>
          )}
          {!b.fresh && (
            <Alert severity="warning">
              Refreshing branch observations. Wait for the latest commit before
              building.
            </Alert>
          )}
          <BranchPicker
            disabled={locked}
            options={Object.keys(b.branches).sort()}
            value={branch || null}
            onChange={(event, value) => choose(value)}
            label="Build branch"
            error={!!branch && !b.branches[branch]}
            helperText={
              branch && !b.branches[branch]
                ? "Choose an available branch."
                : undefined
            }
            onRefresh={() =>
              api("action", {
                action: "refresh-branches",
                project: context.project,
              }).catch((error) => setError(error.message))
            }
          />
          <Box>
            <Typography color="text.secondary" fontSize={12}>
              Commit to build
            </Typography>
            <Typography
              component="code"
              sx={{ overflowWrap: "anywhere", fontSize: 12 }}
            >
              {selection?.sha || "Select a branch"}
            </Typography>
          </Box>
          {changed && (
            <Alert severity="warning">
              The branch changed. Review the latest commit.
            </Alert>
          )}
          {retry && (
            <Typography color="text.secondary" fontSize={13}>
              This builds the latest commit on this branch, which may differ
              from the original run.
            </Typography>
          )}
          <RadioGroup
            value={mode}
            onChange={(e) => {
              setMode(e.target.value);
              setRequestId(crypto.randomUUID());
            }}
          >
            <FormControlLabel
              value="deploy"
              control={<Radio disabled={locked} />}
              label="Build, test & deploy to production"
            />
            <FormControlLabel
              value="validate"
              control={<Radio disabled={locked} />}
              label="Build & test only — keep production unchanged"
            />
          </RadioGroup>
          <Typography color="text.secondary" fontSize={12}>
            Manual branch selection does not change the automatic deployment
            branch. Runs execute one at a time.
          </Typography>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ p: 3 }}>
        {changed && (
          <Button disabled={locked} onClick={() => choose(branch)}>
            Use latest commit
          </Button>
        )}
        <Button disabled={busy} onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="contained"
          disabled={
            busy ||
            !p.build_ready ||
            (!submitted &&
              (!selection?.sha || !b.fresh || !!changed || !b.branches[branch]))
          }
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              const payload =
                submitted ||
                buildPayload({
                  project: context.project,
                  branch,
                  sha: selection.sha,
                  mode,
                  retry,
                  requestId,
                });
              setSubmitted(payload);
              const r = await api("action", payload);
              notify(r.message);
              onClose();
              navigate("job/" + r.job_id);
              refresh().catch(() => {});
            } catch (e) {
              setError(e.message);
              if (e.status && e.status < 500) setSubmitted(null);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy
            ? "Queuing…"
            : submitted
              ? "Check original request"
              : mode === "deploy"
                ? "Queue deployment"
                : "Queue build & tests"}
        </Button>
      </DialogActions>
    </Dialog>
  );
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
      <Typography variant="h4">Services</Typography>
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
          <Box key={id} sx={{ mb: 4 }}>
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
          {(data.snapshots.containers?.value || []).length} services observed
        </Button>
        <span>{active.length} active / queued runs</span>
      </div>
      {active.length > 0 && (
        <Box sx={{ mb: 4 }}>
          <Typography variant="h6">Active deployments</Typography>
          <RunList jobs={active} />
        </Box>
      )}
      {issues.length > 0 && (
        <Box sx={{ mb: 4 }}>
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
