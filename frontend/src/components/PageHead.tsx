import { Breadcrumbs, PageHeader } from "@primer/react";
import { useEffect, type ReactNode } from "react";
import { Link } from "react-router-dom";

export interface Crumb {
  label: string;
  /** Omit on the last (current) item. */
  to?: string;
}

export function useDocumentTitle(name: string): void {
  useEffect(() => {
    document.title = `${name} · Jendock`;
  }, [name]);
}

/**
 * GitHub's page header: breadcrumbs, a title with optional leading/trailing visuals, actions on
 * the right, a description and an optional navigation strip (UnderlineNav). Also sets the
 * document title from `docTitle`, the title text or the last breadcrumb.
 */
export function PageHead({
  crumbs = [],
  title,
  docTitle,
  leading,
  trailing,
  actions,
  description,
  navigation,
}: {
  crumbs?: Crumb[];
  title: string;
  docTitle?: string;
  leading?: ReactNode;
  trailing?: ReactNode;
  actions?: ReactNode;
  description?: ReactNode;
  navigation?: ReactNode;
}) {
  useDocumentTitle(docTitle ?? title);

  return (
    <PageHeader hasBorder={navigation === undefined}>
      {crumbs.length > 0 ? (
        <PageHeader.ContextArea hidden={false}>
          <PageHeader.ContextBar hidden={false}>
            <Breadcrumbs>
              {crumbs.map((crumb, index) =>
                crumb.to !== undefined && index < crumbs.length - 1 ? (
                  <Breadcrumbs.Item key={`${crumb.label}-${index}`} as={Link} to={crumb.to}>
                    {crumb.label}
                  </Breadcrumbs.Item>
                ) : (
                  <Breadcrumbs.Item key={`${crumb.label}-${index}`} href="" selected>
                    {crumb.label}
                  </Breadcrumbs.Item>
                ),
              )}
            </Breadcrumbs>
          </PageHeader.ContextBar>
        </PageHeader.ContextArea>
      ) : null}
      <PageHeader.TitleArea>
        {leading ? <PageHeader.LeadingVisual>{leading}</PageHeader.LeadingVisual> : null}
        <PageHeader.Title as="h1">{title}</PageHeader.Title>
        {trailing ? <PageHeader.TrailingVisual>{trailing}</PageHeader.TrailingVisual> : null}
      </PageHeader.TitleArea>
      {actions ? <PageHeader.Actions>{actions}</PageHeader.Actions> : null}
      {description ? <PageHeader.Description>{description}</PageHeader.Description> : null}
      {navigation ? <PageHeader.Navigation>{navigation}</PageHeader.Navigation> : null}
    </PageHeader>
  );
}
