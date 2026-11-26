import { useState, type ReactElement, type ReactNode } from "react";
import { AlertDialog, Tooltip } from "radix-ui";
import { AlertCircle, Check, Copy, LoaderCircle, X } from "lucide-react";
import { message } from "./api";

export function Loading({ label = "Loading data" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={18} />
      {label}...
    </div>
  );
}
export function Notice({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "error" | "success";
}) {
  return (
    <div
      className={`notice ${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      {tone === "success" ? <Check size={17} /> : <AlertCircle size={17} />}
      <div>{children}</div>
    </div>
  );
}
export function Failure({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <Notice tone="error">
      {message(error)}{" "}
      {retry && (
        <button className="text-button" onClick={retry}>
          Try again
        </button>
      )}
    </Notice>
  );
}
export function Panel({
  title,
  description,
  action,
  children,
  className = "",
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <div className="panel-heading">
          <div>
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export function Badge({
  children,
  tone = "",
  help,
}: {
  children: ReactNode;
  tone?: string;
  help?: string;
}) {
  const explanation =
    help ??
    (typeof children === "string"
      ? badgeHelp[children.toLowerCase()]
      : undefined);
  return explanation ? (
    <Hint content={explanation}>
      <button type="button" className={`badge badge-help ${tone}`}>
        {children}
      </button>
    </Hint>
  ) : (
    <span className={`badge ${tone}`}>{children}</span>
  );
}
const badgeHelp: Record<string, string> = {
  admin:
    "Admins can manage managers, chat policies and members, and view or export usage. Admin accounts are managed in configuration.",
  "protected admin":
    "Configured admins cannot be removed or demoted here. They can manage managers, chat policies and members, and view or export usage.",
  manager:
    "Managers can manage group members, and view or export usage. They cannot manage admins or other managers.",
  member:
    "A member is saved for this chat only. Membership never grants dashboard access or cloud/tool permissions.",
  "all users":
    "Everyone can pass this policy. Saved members are preserved for later.",
  "specific users":
    "Only saved members and inherited admins/managers can pass this policy.",
  "saved grant · inherited access":
    "This saved membership is retained, but the person now inherits access as an admin or manager. Their saved grant cannot be changed while that role is active.",
  recorded:
    "Provider token usage was recorded for this session. Reasoning tokens are already included in output tokens.",
  "usage unavailable":
    "Provider token counts were not available for this recorded session. This does not establish zero or free usage.",
  succeeded:
    "The tool invocation recorded a successful outcome. This is not a billing or distinct SQL-job count.",
  incomplete:
    "No terminal outcome was recorded. The invocation is not necessarily still running.",
  failed:
    "The tool invocation recorded a failure. Provider work may still have occurred.",
  denied: "The tool invocation was denied by an authorization or access check.",
  refused: "The tool invocation was refused by a validation or query guard.",
};
export function Hint({
  content,
  children,
}: {
  content: ReactNode;
  children: ReactElement;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Tooltip.Root open={open} onOpenChange={setOpen}>
      <Tooltip.Trigger
        asChild
        onClick={(event) => {
          event.preventDefault();
          setOpen(true);
        }}
      >
        {children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          className="tooltip"
          sideOffset={7}
          collisionPadding={12}
        >
          {content}
          <Tooltip.Arrow className="tooltip-arrow" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
export function CopyId({ value }: { value: string }) {
  const [result, setResult] = useState("");
  return (
    <span className="copy-id">
      <code>{value}</code>
      <Hint content="Copy this ID. Copying does not change access or open its settings.">
        <button
          className="icon-button"
          aria-label={`Copy ${value}`}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value);
              setResult("Copied");
            } catch {
              setResult("Select the ID to copy it manually");
            }
            setTimeout(() => setResult(""), 3000);
          }}
        >
          <Copy size={13} />
        </button>
      </Hint>
      <span className="copy-result" role="status">
        {result}
      </span>
    </span>
  );
}
export function Confirm({
  label,
  title,
  children,
  onConfirm,
  disabled = false,
}: {
  label: string;
  title: string;
  children: ReactNode;
  onConfirm: () => Promise<unknown>;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(value) => {
        if (!busy) {
          setOpen(value);
          setError(undefined);
        }
      }}
    >
      <AlertDialog.Trigger asChild>
        <button className="text-button danger" disabled={disabled}>
          {label}
        </button>
      </AlertDialog.Trigger>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="overlay" />
        <AlertDialog.Content className="dialog">
          <AlertDialog.Title>{title}</AlertDialog.Title>
          <AlertDialog.Description>{children}</AlertDialog.Description>
          {error ? <Failure error={error} /> : null}
          <div className="dialog-actions">
            <AlertDialog.Cancel asChild>
              <button disabled={busy}>Cancel</button>
            </AlertDialog.Cancel>
            <button
              className="danger-button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError(undefined);
                try {
                  await onConfirm();
                  setOpen(false);
                } catch (err) {
                  setError(err);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Saving..." : label}
            </button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [success, setSuccess] = useState("");
  async function run(action: () => Promise<unknown>, text: string) {
    setBusy(true);
    setError(undefined);
    setSuccess("");
    try {
      await action();
      setSuccess(text);
      return true;
    } catch (err) {
      setError(err);
      return false;
    } finally {
      setBusy(false);
    }
  }
  return {
    busy,
    run,
    feedback: (
      <>
        {error ? <Failure error={error} /> : null}{" "}
        {success && (
          <div className="success-line" role="status">
            <Check size={15} />
            {success}
            <button
              className="icon-button"
              aria-label="Dismiss message"
              onClick={() => setSuccess("")}
            >
              <X size={13} />
            </button>
          </div>
        )}
      </>
    ),
  };
}
export const number = (value: number | null | undefined) =>
  value == null
    ? "Unavailable"
    : new Intl.NumberFormat("en", { maximumFractionDigits: 2 }).format(value);
export const shortNumber = (value: number | null | undefined) =>
  value == null
    ? "Unavailable"
    : new Intl.NumberFormat("en", {
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(value);
export function time(value?: string | null) {
  return value
    ? new Date(value).toISOString().replace("T", " ").slice(0, 19)
    : "Unavailable";
}
