import { Heading, SegmentedControl } from "@primer/react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";

/** Heading row of the per-environment tabs: title and subtitle left, switcher and actions right. */
export function EnvToolbar({
  title,
  subtitle,
  env,
  pathFor,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  env: EnvironmentName;
  pathFor: (env: EnvironmentName) => string;
  actions?: ReactNode;
}) {
  const navigate = useNavigate();
  return (
    <div className={box.spread}>
      <div>
        <Heading as="h2" variant="medium">
          {title}
        </Heading>
        {subtitle ? <p className="muted">{subtitle}</p> : null}
      </div>
      <div className={box.cluster}>
        <SegmentedControl
          aria-label="Environment"
          onChange={(index) => {
            const next = ENVIRONMENTS[index];
            if (next && next !== env) void navigate(pathFor(next));
          }}
        >
          {ENVIRONMENTS.map((name) => (
            <SegmentedControl.Button key={name} selected={name === env}>
              {capitalize(name)}
            </SegmentedControl.Button>
          ))}
        </SegmentedControl>
        {actions}
      </div>
    </div>
  );
}
