import { useRef, useState } from "react";
import { Dialog } from "radix-ui";
import { ArrowLeft, Plus, X } from "lucide-react";
import { Badge, CopyId, Failure, Loading, Notice } from "./components";
import { Identity, useDirectory } from "./directory";
import type { Target } from "./types";

// Match the API's opaque group-ID grammar; suggestions require Lark's oc_ prefix
// so an ordinary group-name search is not mistaken for an ID.
export const validGroupId = (value: string) =>
  /^[A-Za-z0-9_-]{1,256}$/.test(value);
export const suggestedGroupId = (value: string) =>
  /^oc_[A-Za-z0-9_-]+$/.test(value) && validGroupId(value);

type Props = {
  kind: "group" | "member" | "manager";
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialValue?: string;
  conflict: (value: string) => string | undefined;
  onAdd: (value: string) => Promise<unknown>;
  scope?: string;
};
export function AddDialog(props: Props) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog.Root
      open={props.open}
      onOpenChange={(value) => {
        if (!busy) props.onOpenChange(value);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="overlay" />
        <Dialog.Content
          className="dialog add-dialog"
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <Dialog.Title>Add {props.kind}</Dialog.Title>
          <Dialog.Description>
            Review the {props.kind === "group" ? "group" : "person"} and access
            details before adding.
          </Dialog.Description>
          <Dialog.Close
            className="icon-button modal-close"
            aria-label="Close add dialog"
            disabled={busy}
          >
            <X size={18} />
          </Dialog.Close>
          <AddContent {...props} busy={busy} setBusy={setBusy} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
function AddContent({
  kind,
  initialValue = "",
  conflict,
  onAdd,
  onOpenChange,
  scope,
  busy,
  setBusy,
}: Props & { busy: boolean; setBusy: (busy: boolean) => void }) {
  const [value, setValue] = useState(initialValue);
  const [review, setReview] = useState("");
  const [invalid, setInvalid] = useState("");
  const [error, setError] = useState<unknown>();
  const [acknowledged, setAcknowledged] = useState(false);
  const sending = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const target: Target = {
    kind: kind === "group" ? "group" : "email",
    key: review,
  };
  const directory = useDirectory(review ? [target] : []);
  const metadata = directory.get(target);
  const pending =
    !metadata || metadata.metadata_status === "pending" || metadata.refreshing;
  const resolved = metadata?.metadata_status === "available" && !!metadata.name;
  const mismatch =
    kind !== "group" &&
    !!metadata?.directory_email &&
    metadata.directory_email !== review;
  const requiresAcknowledgement = !resolved || mismatch;
  const duplicate = review ? conflict(review) : undefined;
  const scopeDescription =
    kind === "manager"
      ? "This person can manage group access and view/export usage. They cannot manage managers or configured admins."
      : kind === "group"
        ? "The group starts in All users mode. Conversation recall and passive learning are enabled. Workspace admins can change these in Knowledge."
        : `Membership applies only to ${scope || "this chat"}. It does not grant dashboard access or cloud/tool permissions. Saved members do not restrict All users mode.`;
  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        if (sending.current) return;
        if (!review) {
          const normalized =
            kind === "group" ? value.trim() : value.trim().toLowerCase();
          const problem =
            kind === "group" && !validGroupId(normalized)
              ? "Enter a group ID using letters, digits, underscores or hyphens (up to 256 characters)."
              : conflict(normalized);
          if (problem) {
            setInvalid(problem);
            return;
          }
          setInvalid("");
          setError(undefined);
          setAcknowledged(false);
          setReview(normalized);
          requestAnimationFrame(() => heading.current?.focus());
          return;
        }
        if (pending || duplicate || (requiresAcknowledgement && !acknowledged))
          return;
        sending.current = true;
        setBusy(true);
        setError(undefined);
        try {
          await onAdd(review);
          onOpenChange(false);
        } catch (err) {
          setError(err);
        } finally {
          sending.current = false;
          setBusy(false);
        }
      }}
    >
      {!review ? (
        <div className="add-entry">
          <label>
            {kind === "group" ? "Lark group ID" : "Email address"}
            <input
              autoFocus
              type={kind === "group" ? "text" : "email"}
              required
              maxLength={kind === "group" ? 256 : 254}
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                setInvalid("");
              }}
              placeholder={
                kind === "group" ? "oc_example" : "person@example.com"
              }
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
            />
          </label>
          <p className="muted">
            {kind === "group"
              ? "Paste the ID from Lark. We will look up the group before you confirm."
              : "Enter the policy email. We will look up the person before you confirm."}
          </p>
          {invalid && <Notice tone="error">{invalid}</Notice>}
        </div>
      ) : (
        <div className="add-review">
          <h3 ref={heading} tabIndex={-1}>
            Confirm {kind === "group" ? "group" : "person"}
          </h3>
          <div className="review-identity">
            <Identity
              target={target}
              metadata={metadata}
              refresh={directory.refresh}
            />
            {kind === "group" && <CopyId value={review} />}
          </div>
          <div className="access-summary">
            <Badge>
              {kind === "group"
                ? "All users"
                : kind === "manager"
                  ? "Manager"
                  : "Member"}
            </Badge>
            <p>{scopeDescription}</p>
          </div>
          {kind !== "group" && (
            <p className="grant-key">
              Access will be saved for <strong>{review}</strong>.
            </p>
          )}
          {pending ? (
            <Loading label="Looking up directory details" />
          ) : (
            requiresAcknowledgement && (
              <>
                <Notice>
                  {mismatch
                    ? "The directory lists a different email. The entered policy email will not be replaced."
                    : metadata?.metadata_status === "stale"
                      ? "These are last-known details. The directory could not confirm they are current."
                      : "The directory could not confirm a name for this ID or email. Check the entered value before continuing."}
                </Notice>
                <label className="acknowledgement">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    disabled={busy}
                    onChange={(e) => setAcknowledged(e.target.checked)}
                  />
                  <span>
                    I have checked this exact{" "}
                    {kind === "group" ? "group ID" : "email"} and want to add
                    it.
                  </span>
                </label>
              </>
            )
          )}
          {duplicate && <Notice tone="error">{duplicate}</Notice>}
          {error ? <Failure error={error} /> : null}
        </div>
      )}
      <div className="dialog-actions">
        {review ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setReview("");
              setAcknowledged(false);
              setError(undefined);
            }}
          >
            <ArrowLeft size={14} />
            Edit {kind === "group" ? "ID" : "email"}
          </button>
        ) : (
          <Dialog.Close asChild>
            <button type="button">Cancel</button>
          </Dialog.Close>
        )}
        <button
          className="primary"
          disabled={
            busy ||
            (!review
              ? !value.trim()
              : pending ||
                !!duplicate ||
                (requiresAcknowledgement && !acknowledged))
          }
        >
          {busy ? (
            "Adding..."
          ) : review ? (
            <>
              <Plus size={15} />
              Add {kind}
            </>
          ) : (
            "Continue"
          )}
        </button>
      </div>
    </form>
  );
}
