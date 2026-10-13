import type { Icon } from "@primer/octicons-react";
import { NavList, PageLayout } from "@primer/react";
import { Link, Outlet, useLocation } from "react-router-dom";

export interface SettingsItem {
  to: string;
  label: string;
  icon: Icon;
}

/** GitHub's repository settings frame: a NavList on the left, the selected page on the right. */
export function SettingsLayout({ label, items }: { label: string; items: SettingsItem[] }) {
  const { pathname } = useLocation();
  return (
    <PageLayout containerWidth="full" padding="none" columnGap="normal" rowGap="normal">
      <PageLayout.Pane position="start" width="small" padding="none" divider="none">
        <NavList aria-label={label}>
          {items.map((item) => {
            const Visual = item.icon;
            return (
              <NavList.Item
                key={item.to}
                as={Link}
                to={item.to}
                aria-current={pathname === item.to ? "page" : undefined}
              >
                <NavList.LeadingVisual>
                  <Visual />
                </NavList.LeadingVisual>
                {item.label}
              </NavList.Item>
            );
          })}
        </NavList>
      </PageLayout.Pane>
      <PageLayout.Content as="div" padding="none">
        <Outlet />
      </PageLayout.Content>
    </PageLayout>
  );
}
