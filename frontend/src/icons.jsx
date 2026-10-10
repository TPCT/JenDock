import React from "react";
import { Box } from "@mui/material";
import {
  AlertIcon,
  InfoIcon,
  AppsIcon,
  RepoIcon,
  HistoryIcon,
  TerminalIcon,
  SlidersIcon,
  SearchIcon,
  ThreeBarsIcon,
  XIcon,
  ArrowRightIcon,
  SyncIcon,
  RocketIcon,
  CheckCircleFillIcon,
  XCircleFillIcon,
  ClockIcon,
  SkipIcon,
  GitBranchIcon,
  ChevronRightIcon,
  ServerIcon,
  MoonIcon,
  SunIcon,
  SignOutIcon,
  EyeIcon,
  EyeClosedIcon,
  PlayIcon,
  StopIcon,
  PlusIcon,
  WorkflowIcon,
  GitCommitIcon,
} from "@primer/octicons-react";

// Keep the UI's existing sizing contract while rendering official GitHub Octicons.
function adapt(Icon) {
  return function ProductIcon({ sx, fontSize, ...props }) {
    return (
      <Box
        component={Icon}
        size={20}
        aria-hidden="true"
        {...props}
        sx={{
          fontSize: fontSize === "small" ? 16 : 20,
          width: "1em",
          height: "1em",
          flexShrink: 0,
          verticalAlign: "text-bottom",
          ...sx,
        }}
      />
    );
  };
}
export const GridView = adapt(AppsIcon);
export const Folder = adapt(RepoIcon);
export const History = adapt(HistoryIcon);
export const Terminal = adapt(TerminalIcon);
export const Settings = adapt(SlidersIcon);
export const Search = adapt(SearchIcon);
export const Menu = adapt(ThreeBarsIcon);
export const Close = adapt(XIcon);
export const Arrow = adapt(ArrowRightIcon);
export const Refresh = adapt(SyncIcon);
export const Rocket = adapt(RocketIcon);
export const Check = adapt(CheckCircleFillIcon);
export const ErrorIcon = adapt(XCircleFillIcon);
export const PendingIcon = adapt(ClockIcon);
export const SkippedIcon = adapt(SkipIcon);
export const Branch = adapt(GitBranchIcon);
export const Chevron = adapt(ChevronRightIcon);
export const Dns = adapt(ServerIcon);
export const Dark = adapt(MoonIcon);
export const Light = adapt(SunIcon);
export const Logout = adapt(SignOutIcon);
export const Visibility = adapt(EyeIcon);
export const VisibilityOff = adapt(EyeClosedIcon);
export const PlayArrow = adapt(PlayIcon);
export const StopCircleOutlined = adapt(StopIcon);
export const Add = adapt(PlusIcon);
export const Workflow = adapt(WorkflowIcon);
export const CommitIcon = adapt(GitCommitIcon);

export const WarningIcon = adapt(AlertIcon);
export const Info = adapt(InfoIcon);
