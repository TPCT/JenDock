import { SearchIcon } from "@primer/octicons-react";
import { Button } from "@primer/react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";

/** GitHub's 404: shown for every address that matches no page, inside or outside a project. */
export function NotFoundPage() {
  useDocumentTitle("Page not found");
  return (
    <EmptyState
      icon={<SearchIcon size={32} aria-hidden="true" />}
      title="Page not found"
      action={
        <Button as={Link} to="/projects" variant="primary">
          Go to Projects
        </Button>
      }
    >
      This address does not match any page. Check the link for typing mistakes, or start again from
      the project list.
    </EmptyState>
  );
}
