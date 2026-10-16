import { Button, Checkbox, FormControl, Select, TextInput } from "@primer/react";
import { useState } from "react";
import {
  useS3BackupSettings,
  useSaveS3BackupSettings,
  useTestS3BackupSettings,
} from "../api/backupHooks";
import type { S3BackupSettingsBody } from "../api/backupTypes";
import { CopyField } from "../components/CopyField";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsSection } from "../components/SettingsSection";
import box from "../styles/box.module.css";

const DEFAULTS: S3BackupSettingsBody = {
  access_key_id: "",
  bucket: "aic-consultation",
  region: "eu-west-3",
  prefix: "backups",
  hour: 3,
  enabled: false,
  mysql_image: "mysql:8.4",
  mariadb_image: "mariadb:11.4",
  postgres_image: "postgres:17",
};

export function S3BackupsSection() {
  const query = useS3BackupSettings();
  const save = useSaveS3BackupSettings();
  const test = useTestS3BackupSettings();
  const [overrides, setOverrides] = useState<Partial<S3BackupSettingsBody>>({});
  const values = { ...DEFAULTS, ...query.data, ...overrides };
  const [secret, setSecret] = useState("");

  const field = (name: keyof S3BackupSettingsBody, label: string, type = "text") => (
    <FormControl key={name}>
      <FormControl.Label>{label}</FormControl.Label>
      <TextInput
        block
        type={type}
        value={String(values[name] ?? "")}
        onChange={(event) => {
          setOverrides((current) => ({ ...current, [name]: event.target.value }));
        }}
      />
    </FormControl>
  );

  if (query.isPending) return <Loading label="Loading S3 backup settings" />;
  if (query.isError) {
    return (
      <ErrorCallout
        title="Could not load S3 backup settings"
        error={query.error}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <SettingsSection
      title="S3 backups"
      intro="Store one verified daily dump per database connection. The previous object is retained if a new backup fails."
    >
      <div className={box.stack}>
        {field("access_key_id", "AWS access key ID")}
        <FormControl>
          <FormControl.Label>AWS secret access key</FormControl.Label>
          <TextInput
            block
            type="password"
            autoComplete="new-password"
            value={secret}
            placeholder={
              query.data.secret_configured
                ? "Saved; leave blank to keep it"
                : "Enter the secret key"
            }
            onChange={(event) => {
              setSecret(event.target.value);
            }}
          />
          <FormControl.Caption>
            This key is write-only and encrypted by the worker.
          </FormControl.Caption>
        </FormControl>
        {field("bucket", "S3 bucket")}
        {field("region", "AWS region")}
        {field("prefix", "Object prefix")}
        <FormControl>
          <FormControl.Label>Daily schedule (Africa/Cairo)</FormControl.Label>
          <Select
            block
            value={String(values.hour)}
            onChange={(event) => {
              setOverrides((v) => ({ ...v, hour: Number(event.target.value) }));
            }}
          >
            {Array.from({ length: 24 }, (_, hour) => (
              <Select.Option key={hour} value={String(hour)}>
                {String(hour).padStart(2, "0")}:00
              </Select.Option>
            ))}
          </Select>
        </FormControl>
        {field("mysql_image", "MySQL dump image")}
        {field("mariadb_image", "MariaDB dump image")}
        {field("postgres_image", "PostgreSQL dump image")}
        <FormControl>
          <Checkbox
            type="checkbox"
            checked={values.enabled}
            onChange={(event) => {
              setOverrides((v) => ({ ...v, enabled: event.target.checked }));
            }}
          />
          <FormControl.Label>Enable daily scheduled backups</FormControl.Label>
        </FormControl>
        <CopyField label="Least-privilege IAM policy" value={query.data.iam_policy} multiline />
        {save.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(save.error)}
          </Callout>
        ) : null}
        {test.data ? (
          <Callout
            variant={test.data.ok ? "success" : "danger"}
            role={test.data.ok ? "status" : "alert"}
          >
            {test.data.message}
          </Callout>
        ) : null}
        {test.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(test.error)}
          </Callout>
        ) : null}
        <div className={box.cluster}>
          <Button
            variant="primary"
            disabled={save.isPending}
            onClick={() => {
              save.mutate(
                { ...values, ...(secret ? { secret_key: secret } : {}) },
                {
                  onSuccess: () => {
                    setSecret("");
                  },
                },
              );
            }}
          >
            {save.isPending ? "Saving…" : "Save S3 settings"}
          </Button>
          <Button
            disabled={test.isPending || !query.data.secret_configured}
            onClick={() => {
              test.mutate();
            }}
          >
            {test.isPending ? "Testing…" : "Test connection"}
          </Button>
        </div>
      </div>
    </SettingsSection>
  );
}
