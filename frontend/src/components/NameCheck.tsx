import { useState } from "react";
import { Link } from "react-router-dom";
import { useNameLookup, type DomainScope } from "../api/discoveryHooks";
import { useDebouncedEffect } from "../lib/useDebouncedEffect";
import { capitalize } from "../lib/format";
import { useCan } from "../lib/permissions";
import { Callout } from "./Feedback";

const HOSTNAME = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const DELAY_MS = 400;

/**
 * The live check under a host name field: what already exists in Cloudflare for the full name.
 * A record that Jendock did not create is a conflict (applying would stop at it), and a name
 * that already belongs to a project is refused, so both are said before anything is saved.
 */
export function NameCheck({ scope, hostname }: { scope: DomainScope; hostname: string }) {
  const [settled, setSettled] = useState(hostname);
  useDebouncedEffect(hostname, DELAY_MS, setSettled);
  const can = useCan();
  const complete = HOSTNAME.test(settled) && settled === hostname;
  const lookup = useNameLookup(scope, settled, complete);
  if (!complete) return null;
  if (lookup.isPending) return <p className="muted">Checking Cloudflare for {settled}…</p>;
  if (lookup.isError) return null;
  const found = lookup.data;
  if (!found.configured) return null;
  if (found.in_use_by) {
    return (
      <Callout variant="danger" role="alert" title={`${found.hostname} is already used`}>
        It belongs to {found.in_use_by.project} / {capitalize(found.in_use_by.environment)} (
        {found.in_use_by.origin}). A host name can only be linked once.
      </Callout>
    );
  }
  if (found.records.length > 0) {
    const foreign = found.records.filter((record) => !record.managed_by_jendock);
    const summary = found.records
      .map((r) => `${r.record_type} ${r.content}${r.proxied ? ", proxied" : ""}`)
      .join("; ");
    return (
      <Callout
        variant="warning"
        role="alert"
        title={`A DNS record for ${found.hostname} already exists`}
      >
        {summary}.{" "}
        {foreign.length > 0 ? (
          <>
            Jendock never overwrites a record it did not create, so applying this domain would stop
            here. If the site already works, adopt it instead
            {can("domain.discover") ? (
              <>
                {" "}
                on the{" "}
                <Link to={`/domains?q=${encodeURIComponent(found.hostname)}`}>Domains page</Link>
              </>
            ) : null}
            ; otherwise pick another name.
          </>
        ) : (
          "It was created by Jendock earlier."
        )}
      </Callout>
    );
  }
  if (found.message !== "") {
    return (
      <Callout variant="warning" title="Cloudflare cannot take this name">
        {found.message}
      </Callout>
    );
  }
  return (
    <p className="muted" role="status">
      {found.hostname} is free in the {found.zone ?? "Cloudflare"} zone. Jendock will create the DNS
      record (proxied by Cloudflare if you chose that), the Apache site and the certificate.
    </p>
  );
}
