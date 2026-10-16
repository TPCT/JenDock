import { CloudflareSection } from "./CloudflareSection";
import { GithubSection } from "./GithubSection";
import { S3BackupsSection } from "./S3BackupsSection";

/** Settings -> Integrations: the outside services Jendock needs, one section each. */
export function IntegrationsPage() {
  return (
    <>
      <CloudflareSection />
      <GithubSection />
      <S3BackupsSection />
    </>
  );
}
