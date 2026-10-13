import {
  GearIcon,
  MoonIcon,
  PlayIcon,
  RepoIcon,
  SignOutIcon,
  SunIcon,
  ThreeBarsIcon,
  TerminalIcon,
  XIcon,
} from "@primer/octicons-react";
import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useLogout, useSession } from "../api/hooks";
import { useTheme } from "../lib/theme";
import { MOBILE_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { Breadcrumbs, CrumbsProvider } from "./Breadcrumbs";
import "../styles/shell.css";

export function Shell() {
  const mobile = useMediaQuery(MOBILE_QUERY);
  const [openState, setOpen] = useState(false);
  // The drawer only exists on mobile; leaving mobile mode closes it for good.
  if (!mobile && openState) setOpen(false);
  const open = mobile && openState;
  const navigate = useNavigate();
  const { theme, toggle } = useTheme();
  const session = useSession();
  const logout = useLogout();
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        menuButton.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const signOut = () => {
    logout.mutate(undefined, {
      onSettled: () => {
        void navigate("/login", { replace: true });
      },
    });
  };

  const nextTheme = theme === "dark" ? "light" : "dark";

  return (
    <CrumbsProvider>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className="shell">
        <aside className="sidebar" data-open={open} id="sidebar" aria-label="Sidebar">
          <div className="sidebar-head">
            <Link to="/projects" className="brand">
              <span className="brand-mark" aria-hidden="true">
                <TerminalIcon size={18} />
              </span>
              Shiftcodes
            </Link>
            {mobile ? (
              <button
                ref={closeButton}
                type="button"
                className="btn btn-invisible btn-icon menu-button"
                onClick={() => {
                  setOpen(false);
                }}
                aria-label="Close navigation"
              >
                <XIcon />
              </button>
            ) : null}
          </div>
          <nav
            aria-label="Main"
            onClick={() => {
              setOpen(false);
            }}
          >
            <ul className="nav">
              <li>
                <NavLink to="/projects">
                  <RepoIcon size={16} aria-hidden="true" />
                  Projects
                </NavLink>
              </li>
              <li>
                <NavLink to="/runs">
                  <PlayIcon size={16} aria-hidden="true" />
                  Runs
                </NavLink>
              </li>
              <li>
                <NavLink to="/settings">
                  <GearIcon size={16} aria-hidden="true" />
                  Settings
                  <span className="nav-soon">Soon</span>
                </NavLink>
              </li>
            </ul>
          </nav>
          <div className="sidebar-footer">
            <div className="sidebar-user">
              <span>Signed in as</span>
              <strong>{session.data?.user.username ?? "..."}</strong>
            </div>
            <button type="button" className="btn" onClick={toggle}>
              {theme === "dark" ? (
                <SunIcon size={16} aria-hidden="true" />
              ) : (
                <MoonIcon size={16} aria-hidden="true" />
              )}
              Switch to {nextTheme} theme
            </button>
            <button type="button" className="btn" onClick={signOut} disabled={logout.isPending}>
              <SignOutIcon size={16} aria-hidden="true" />
              Sign out
            </button>
          </div>
        </aside>
        <div
          className="scrim"
          data-open={open}
          onClick={() => {
            setOpen(false);
          }}
          aria-hidden="true"
        />
        <div className="main-column">
          <header className="topbar">
            {mobile ? (
              <button
                ref={menuButton}
                type="button"
                className="btn btn-invisible btn-icon menu-button"
                onClick={() => {
                  setOpen(true);
                }}
                aria-label="Open navigation"
                aria-controls="sidebar"
                aria-expanded={open}
              >
                <ThreeBarsIcon />
              </button>
            ) : null}
            <Breadcrumbs />
          </header>
          <main id="main" className="content" tabIndex={-1}>
            <Outlet />
          </main>
        </div>
      </div>
    </CrumbsProvider>
  );
}
