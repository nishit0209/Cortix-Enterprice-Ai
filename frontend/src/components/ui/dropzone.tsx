"use client";

import * as React from "react";
import { FileUp, UploadCloud } from "lucide-react";
import { cn } from "@/lib/utils";

export interface DropzoneProps {
  accept?: string[];
  maxBytes?: number;
  onFile: (file: File) => void;
  onReject?: (reason: string) => void;
  disabled?: boolean;
  className?: string;
  hint?: React.ReactNode;
}

export function validateFile(file: File, accept?: string[], maxBytes?: number): string | null {
  if (accept && accept.length > 0) {
    const name = file.name.toLowerCase();
    if (!accept.some((ext) => name.endsWith(ext))) return `Unsupported file type. Use ${accept.join(", ")}.`;
  }
  if (maxBytes && file.size > maxBytes) return `File is too large (max ${Math.round(maxBytes / 1024 / 1024)} MB).`;
  if (file.size === 0) return "The file is empty.";
  return null;
}

/** Drag-and-drop file picker (keyboard accessible: Enter/Space opens the file dialog). */
export function Dropzone({ accept, maxBytes, onFile, onReject, disabled, className, hint }: DropzoneProps) {
  const inputRef = React.useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = React.useState(false);
  const depth = React.useRef(0);

  const handle = (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    const err = validateFile(file, accept, maxBytes);
    if (err) onReject?.(err);
    else onFile(file);
  };

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      aria-label="Choose a file to upload or drag and drop it here"
      onClick={() => !disabled && inputRef.current?.click()}
      onKeyDown={(e) => {
        if (!disabled && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragEnter={(e) => {
        e.preventDefault();
        depth.current++;
        if (!disabled) setDragging(true);
      }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={(e) => {
        e.preventDefault();
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        if (!disabled) handle(e.dataTransfer.files);
      }}
      className={cn(
        "group relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-card border border-dashed border-border-strong bg-surface-2/50 px-6 py-10 text-center transition-[border-color,background-color] duration-150 focus-ring",
        "hover:border-accent/60 hover:bg-accent-soft/40",
        dragging && "border-accent bg-accent-soft/60",
        disabled && "pointer-events-none opacity-60",
        className,
      )}
    >
      <span
        className={cn(
          "flex size-11 items-center justify-center rounded-card border border-border bg-surface text-fg-muted shadow-soft transition-transform duration-200",
          dragging && "-translate-y-0.5 text-accent",
        )}
      >
        {dragging ? <FileUp className="size-5" /> : <UploadCloud className="size-5" />}
      </span>
      <div className="space-y-1">
        <p className="text-sm font-medium text-fg">
          {dragging ? "Drop to upload" : (
            <>
              Drag and drop a file, or <span className="text-accent">browse</span>
            </>
          )}
        </p>
        {hint && <p className="text-xs text-fg-muted">{hint}</p>}
      </div>
      <input
        ref={inputRef}
        type="file"
        className="sr-only"
        tabIndex={-1}
        accept={accept?.join(",")}
        onChange={(e) => {
          handle(e.target.files);
          e.target.value = "";
        }}
        data-testid="dropzone-input"
      />
    </div>
  );
}
