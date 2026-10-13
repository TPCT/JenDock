import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

export interface Crumb {
  label: string;
  /** Omit on the last (current) item. */
  to?: string;
}

interface CrumbsContextValue {
  crumbs: Crumb[];
  setCrumbs: (crumbs: Crumb[]) => void;
}

const CrumbsContext = createContext<CrumbsContextValue | null>(null);

export function CrumbsProvider({ children }: { children: ReactNode }) {
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  const value = useMemo(() => ({ crumbs, setCrumbs }), [crumbs]);
  return <CrumbsContext value={value}>{children}</CrumbsContext>;
}

/** Declares the page's breadcrumb trail; the last item also becomes the document title. */
export function useCrumbs(crumbs: Crumb[]): void {
  const context = useContext(CrumbsContext);
  const key = JSON.stringify(crumbs);
  const setCrumbs = context?.setCrumbs;
  useEffect(() => {
    const parsed = JSON.parse(key) as Crumb[];
    setCrumbs?.(parsed);
    const last = parsed[parsed.length - 1];
    document.title = last ? `${last.label} · Shiftcodes` : "Shiftcodes";
    return () => {
      setCrumbs?.([]);
    };
  }, [key, setCrumbs]);
}

export function Breadcrumbs() {
  const crumbs = useContext(CrumbsContext)?.crumbs ?? [];
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      <ol>
        {crumbs.map((crumb, index) => {
          const last = index === crumbs.length - 1;
          return (
            <li key={`${crumb.label}-${index}`}>
              {crumb.to !== undefined && !last ? (
                <Link to={crumb.to}>{crumb.label}</Link>
              ) : (
                <span aria-current={last ? "page" : undefined}>{crumb.label}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
