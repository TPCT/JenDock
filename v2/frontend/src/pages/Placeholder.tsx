import { SearchIcon } from "@primer/octicons-react";
import { Button } from "@primer/react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";

export function NotFoundPage() {
  useDocumentTitle("Page not found");
  return (
    <EmptyState
      icon={<SearchIcon size={32} aria-hidden="true" />}
      title="Page not found"
      action={
        <Button as={Link} to="/projects">
          Go to projects
        </Button>
      }
    >
      This address does not match any page. Check the link or start from the project list.
    </EmptyState>
  );
}
