"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "./button";
import { Dialog, DialogBody, DialogClose, DialogContent, DialogFooter, DialogHeader } from "./dialog";
import { Input } from "./input";
import { Label } from "./field";

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "default";
  /** Require typing this exact text to enable the confirm button (for irreversible actions). */
  confirmText?: string;
  loading?: boolean;
  onConfirm: () => void | Promise<unknown>;
  children?: React.ReactNode;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "danger",
  confirmText,
  loading,
  onConfirm,
  children,
}: ConfirmDialogProps) {
  const [typed, setTyped] = React.useState("");
  const inputId = React.useId();
  React.useEffect(() => {
    if (!open) setTyped("");
  }, [open]);
  const blocked = !!confirmText && typed.trim() !== confirmText;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm" role="alertdialog">
        <DialogHeader
          title={title}
          description={description}
          icon={tone === "danger" ? <AlertTriangle className="text-danger" /> : undefined}
        />
        {(children || confirmText) && (
          <DialogBody className="space-y-3">
            {children}
            {confirmText && (
              <div className="space-y-1.5">
                <Label htmlFor={inputId}>
                  Type <span className="font-mono text-fg">{confirmText}</span> to confirm
                </Label>
                <Input id={inputId} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
              </div>
            )}
          </DialogBody>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary" disabled={loading}>
              {cancelLabel}
            </Button>
          </DialogClose>
          <Button
            variant={tone === "danger" ? "danger" : "primary"}
            loading={loading}
            disabled={blocked}
            onClick={() => void onConfirm()}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
