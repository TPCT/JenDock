import { FileDirectoryFillIcon, FileIcon } from "@primer/octicons-react";
import { Breadcrumbs, Button, Select } from "@primer/react";
import { useState } from "react";
import { useDirectory, useFile } from "../../api/containerHooks";
import type { ContainerView } from "../../api/containerTypes";
import type { EnvironmentName } from "../../api/types";
import { Callout, ErrorCallout, Loading, errorMessage } from "../../components/Feedback";
import { formatBytes } from "../../lib/format";
import box from "../../styles/box.module.css";
import styles from "./Files.module.css";

const SIZES = [16_384, 65_536, 262_144] as const;

function join(parent: string, name: string): string {
  return parent === "/" ? `/${name}` : `${parent}/${name}`;
}

/** Read-only browser of the container's file system. */
export function FilesPanel({
  slug,
  env,
  container,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
}) {
  const [path, setPath] = useState("/");
  const [file, setFile] = useState<string | null>(null);
  const [maxBytes, setMaxBytes] = useState<number>(65_536);
  const running = container.state === "running";
  const dir = useDirectory(slug, env, container.id, path);
  const content = useFile(slug, env, container.id, file, maxBytes);
  const segments = path.split("/").filter(Boolean);

  if (!running) {
    return <Callout>Files can only be browsed while the container is running.</Callout>;
  }

  const open = (target: string) => {
    setPath(target);
    setFile(null);
  };

  return (
    <div className={box.stack}>
      <p className="muted">
        Read only. Files that may hold credentials (.env files, keys, certificates) are hidden, and
        every file you open is recorded in the audit log.
      </p>
      <nav aria-label="Folder path">
        <Breadcrumbs>
          <Breadcrumbs.Item
            href="#"
            selected={segments.length === 0}
            onClick={(event: { preventDefault: () => void }) => {
              event.preventDefault();
              open("/");
            }}
          >
            / (root)
          </Breadcrumbs.Item>
          {segments.map((segment, index) => {
            const target = `/${segments.slice(0, index + 1).join("/")}`;
            return (
              <Breadcrumbs.Item
                key={target}
                href="#"
                selected={index === segments.length - 1}
                onClick={(event: { preventDefault: () => void }) => {
                  event.preventDefault();
                  open(target);
                }}
              >
                {segment}
              </Breadcrumbs.Item>
            );
          })}
        </Breadcrumbs>
      </nav>
      {dir.isPending ? <Loading label="Loading folder" /> : null}
      {dir.isError ? <ErrorCallout title="Could not open this folder" error={dir.error} /> : null}
      {dir.data ? (
        <div className={box.box}>
          {dir.data.entries.length === 0 ? (
            <p className={box.row}>This folder is empty.</p>
          ) : (
            <ul className={box.list} aria-label={`Contents of ${dir.data.path}`}>
              {dir.data.entries.map((entry) => {
                const target = join(path, entry.name);
                const Icon = entry.kind === "directory" ? FileDirectoryFillIcon : FileIcon;
                return (
                  <li key={entry.name} className={box.row}>
                    <button
                      type="button"
                      className={styles.entry}
                      aria-current={file === target ? "true" : undefined}
                      onClick={() => {
                        if (entry.kind === "directory") open(target);
                        else setFile(target);
                      }}
                    >
                      <Icon
                        size={16}
                        className={entry.kind === "directory" ? styles.dir : "muted"}
                        aria-hidden="true"
                      />
                      <span>{entry.name}</span>
                      <span className="muted">
                        {entry.kind === "directory" ? "folder" : "file"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {dir.data.truncated ? (
            <p className={`${box.row} muted`}>
              Only the first 500 entries are shown. Open a more specific folder to see the rest.
            </p>
          ) : null}
        </div>
      ) : null}

      {file !== null ? (
        <section className={box.box} aria-label={`File ${file}`}>
          <div className={box.header}>
            <code>{file}</code>
            <Select
              aria-label="How much of the file to load"
              size="small"
              value={String(maxBytes)}
              onChange={(event) => {
                setMaxBytes(Number(event.target.value));
              }}
            >
              {SIZES.map((size) => (
                <Select.Option key={size} value={String(size)}>
                  Up to {formatBytes(size)}
                </Select.Option>
              ))}
            </Select>
          </div>
          <div className={box.row}>
            {content.isPending ? <Loading label="Loading file" /> : null}
            {content.isError ? (
              <Callout variant="warning" role="alert">
                {errorMessage(content.error)}
              </Callout>
            ) : null}
            {content.data?.binary ? (
              <p>
                This is a binary file ({formatBytes(content.data.size)}). It cannot be shown as
                text.
              </p>
            ) : null}
            {content.data && !content.data.binary ? (
              <>
                <pre className={styles.content} tabIndex={0} aria-label="File content">
                  {content.data.text}
                </pre>
                {content.data.truncated ? (
                  <p className="muted">
                    Showing the first {formatBytes(maxBytes)} of {formatBytes(content.data.size)}.{" "}
                    {maxBytes < 262_144 ? (
                      <Button
                        size="small"
                        onClick={() => {
                          setMaxBytes(SIZES.find((size) => size > maxBytes) ?? 262_144);
                        }}
                      >
                        Load more
                      </Button>
                    ) : (
                      "256 KiB is the most this page can load."
                    )}
                  </p>
                ) : null}
              </>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
