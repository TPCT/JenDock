import { PlusIcon, TrashIcon } from "@primer/octicons-react";
import { Button, IconButton } from "@primer/react";
import type { ReactNode } from "react";
import styles from "./pipeline.module.css";

/** A native fieldset styled like a settings group; `disabled` on a parent fieldset still applies. */
export function Group({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className={styles.group}>
      <legend className={styles.legend}>{legend}</legend>
      {children}
    </fieldset>
  );
}

export function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <IconButton
      icon={TrashIcon}
      variant="danger"
      size="small"
      aria-label={label}
      tooltipDirection="s"
      onClick={onClick}
    />
  );
}

export function AddButton({
  children,
  onClick,
  disabled,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Button size="small" leadingVisual={PlusIcon} onClick={onClick} disabled={disabled ?? false}>
      {children}
    </Button>
  );
}
