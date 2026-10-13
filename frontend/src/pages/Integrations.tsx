import { CloudflareSection } from "./CloudflareSection";
import { GithubSection } from "./GithubSection";

/** Settings -> Integrations: the outside services Jendock needs, one section each. */
export function IntegrationsPage() {
  return (
    <>
      <CloudflareSection />
      <GithubSection />
    </>
  );
}
