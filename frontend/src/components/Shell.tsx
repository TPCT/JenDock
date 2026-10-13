import {
  GearIcon,
  GlobeIcon,
  HistoryIcon,
  HomeIcon,
  KeyIcon,
  PeopleIcon,
  PersonIcon,
  PlayIcon,
  RepoIcon,
  SignOutIcon,
  TerminalIcon,
  ThreeBarsIcon,
} from "@primer/octicons-react";
import {
  ActionList,
  ActionMenu,
  Button,
  Dialog,
  Header,
  IconButton,
  NavList,
  PageLayout,
} from "@primer/react";
import { memo, useRef, useState, type ElementType, type ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useLogout, useSession } from "../api/hooks";
import { THEME_CHOICES, useTheme } from "../lib/theme";
import { useCan } from "../lib/permissions";
import { MOBILE_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { Initials } from "./Initials";
import styles from "./Shell.module.css";

/** Height of the top bar in px; the sidebar fills the screen below it. Keep in sync with Shell.module.css. */
const HEADER_HEIGHT = 64;

function NavItem({
  to,
  icon: Icon,
  children,
  trailing,
  match,
}: {
  to: string;
  icon: ElementType;
  children: ReactNode;
  trailing?: ReactNode;
  match: (pathname: string) => boolean;
}) {
  const { pathname } = useLocation();
  const current = match(pathname);
  return (
    <NavList.Item as={Link} to={to} aria-current={current ? "page" : undefined}>
      <NavList.LeadingVisual>
        <Icon />
      </NavList.LeadingVisual>
      {children}
      {trailing ? <NavList.TrailingVisual>{trailing}</NavList.TrailingVisual> : null}
    </NavList.Item>
  );
}

/** Memoised for the reason given at RunSidebar: Primer re-creates link anchors on every render. */
const MainNav = memo(function MainNav({ onNavigate }: { onNavigate?: () => void }) {
  const showDomains = useCan()("domain.discover");
  return (
    <NavList aria-label="Main" onClick={onNavigate}>
      <NavItem to="/" icon={HomeIcon} match={(path) => path === "/"}>
        Home
      </NavItem>
      <NavItem
        to="/projects"
        icon={RepoIcon}
        match={(path) => path === "/projects" || path.startsWith("/projects/")}
      >
        Projects
      </NavItem>
      <NavItem
        to="/runs"
        icon={PlayIcon}
        match={(path) => path === "/runs" || path.startsWith("/runs/")}
      >
        Runs
      </NavItem>
      {showDomains ? (
        <NavItem to="/domains" icon={GlobeIcon} match={(path) => path === "/domains"}>
          Domains
        </NavItem>
      ) : null}
      <NavItem to="/audit" icon={HistoryIcon} match={(path) => path === "/audit"}>
        Audit log
      </NavItem>
      <NavItem
        to="/settings"
        icon={GearIcon}
        match={(path) => path === "/settings" || path.startsWith("/settings/")}
      >
        Settings
      </NavItem>
    </NavList>
  );
});

function UserMenu() {
  const navigate = useNavigate();
  const session = useSession();
  const logout = useLogout();
  const { choice, setChoice } = useTheme();
  const username = session.data?.user.username ?? "...";
  const can = useCan();
  const role = session.data?.user.role;
  const displayName = session.data?.user.display_name ?? "";
  const shown = displayName === "" ? username : displayName;

  const signOut = () => {
    logout.mutate(undefined, {
      onSettled: () => {
        void navigate("/login", { replace: true });
      },
    });
  };

  return (
    <ActionMenu>
      <ActionMenu.Anchor>
        <Button variant="invisible" className={styles.headerButton} aria-label="User menu">
          <span className={styles.account}>
            <Initials displayName={displayName} username={username} />
            <span className={styles.accountName}>{shown}</span>
          </span>
        </Button>
      </ActionMenu.Anchor>
      <ActionMenu.Overlay width="medium">
        <ActionList>
          <ActionList.Group>
            <ActionList.GroupHeading>
              Signed in as <strong>{username}</strong>
              {role ? <span className={styles.role}> ({role})</span> : null}
            </ActionList.GroupHeading>
          </ActionList.Group>
          <ActionList.LinkItem as={Link} to="/settings/profile">
            <ActionList.LeadingVisual>
              <PersonIcon />
            </ActionList.LeadingVisual>
            Your profile
          </ActionList.LinkItem>
          <ActionList.LinkItem as={Link} to="/settings/account">
            <ActionList.LeadingVisual>
              <KeyIcon />
            </ActionList.LeadingVisual>
            Change password
          </ActionList.LinkItem>
          <ActionList.LinkItem as={Link} to="/settings">
            <ActionList.LeadingVisual>
              <GearIcon />
            </ActionList.LeadingVisual>
            Settings
          </ActionList.LinkItem>
          {can("user.manage") ? (
            <ActionList.LinkItem as={Link} to="/settings/members">
              <ActionList.LeadingVisual>
                <PeopleIcon />
              </ActionList.LeadingVisual>
              Members
            </ActionList.LinkItem>
          ) : null}
          <ActionList.Divider />
          <ActionList.Group selectionVariant="single">
            <ActionList.GroupHeading>Theme</ActionList.GroupHeading>
            {THEME_CHOICES.map((item) => (
              <ActionList.Item
                key={item.value}
                selected={choice === item.value}
                onSelect={() => {
                  setChoice(item.value);
                }}
              >
                {item.label}
              </ActionList.Item>
            ))}
          </ActionList.Group>
          <ActionList.Divider />
          <ActionList.Item onSelect={signOut} disabled={logout.isPending}>
            <ActionList.LeadingVisual>
              <SignOutIcon />
            </ActionList.LeadingVisual>
            Sign out
          </ActionList.Item>
        </ActionList>
      </ActionMenu.Overlay>
    </ActionMenu>
  );
}

/** GitHub's frame: a dark header bar, a NavList in the left pane, the page on the right. */
export function Shell() {
  const mobile = useMediaQuery(MOBILE_QUERY);
  const [openState, setOpen] = useState(false);
  // The drawer only exists on mobile; leaving mobile mode closes it for good.
  if (!mobile && openState) setOpen(false);
  const open = mobile && openState;
  const menuButton = useRef<HTMLButtonElement>(null);

  return (
    <>
      <a className={styles.skip} href="#main">
        Skip to content
      </a>
      <Header className={styles.header}>
        {mobile ? (
          <Header.Item>
            <IconButton
              ref={menuButton}
              variant="invisible"
              className={styles.headerButton}
              icon={ThreeBarsIcon}
              aria-label="Open navigation"
              aria-expanded={open}
              onClick={() => {
                setOpen(true);
              }}
            />
          </Header.Item>
        ) : null}
        <Header.Item>
          <Header.Link as={Link} to="/" className={styles.brand}>
            <TerminalIcon size={24} aria-hidden="true" />
            <span>Jendock</span>
          </Header.Link>
        </Header.Item>
        <Header.Item full />
        <Header.Item>
          <UserMenu />
        </Header.Item>
      </Header>
      <PageLayout containerWidth="full" padding="none" rowGap="none" columnGap="none">
        {mobile ? null : (
          <PageLayout.Pane
            position="start"
            width="small"
            divider="none"
            padding="condensed"
            sticky
            offsetHeader={HEADER_HEIGHT}
            className={styles.pane}
          >
            <MainNav />
          </PageLayout.Pane>
        )}
        <PageLayout.Content as="main" padding="normal">
          <div id="main" tabIndex={-1} className={styles.main}>
            <Outlet />
          </div>
        </PageLayout.Content>
      </PageLayout>
      {open ? (
        <Dialog
          title="Navigation"
          position="left"
          width="small"
          returnFocusRef={menuButton}
          onClose={() => {
            setOpen(false);
          }}
        >
          <MainNav
            onNavigate={() => {
              setOpen(false);
            }}
          />
        </Dialog>
      ) : null}
    </>
  );
}
