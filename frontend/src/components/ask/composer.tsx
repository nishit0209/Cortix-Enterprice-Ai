"use client";

import * as React from "react";
import { ArrowUp, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/feedback";
import { cn } from "@/lib/utils";

function isTypingTarget(t: EventTarget | null) {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

export interface ComposerHandle {
  focus: () => void;
  setValue: (v: string) => void;
}

/**
 * Chat composer. Shortcuts: Enter sends · Shift+Enter new line · ↑ in an empty box recalls the last question ·
 * Esc stops a streaming answer · "/" anywhere focuses the composer.
 */
export const Composer = React.forwardRef<
  ComposerHandle,
  {
    onSend: (text: string) => void;
    onStop?: () => void;
    streaming: boolean;
    disabled?: boolean;
    disabledReason?: string;
    lastQuestion?: string | null;
    placeholder?: string;
    maxLength?: number;
  }
>(function Composer({ onSend, onStop, streaming, disabled, disabledReason, lastQuestion, placeholder, maxLength = 2000 }, ref) {
  const [value, setValue] = React.useState("");
  const taRef = React.useRef<HTMLTextAreaElement>(null);

  React.useImperativeHandle(ref, () => ({
    focus: () => taRef.current?.focus(),
    setValue: (v: string) => {
      setValue(v);
      requestAnimationFrame(() => taRef.current?.focus());
    },
  }));

  React.useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "0px";
    ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
  }, [value]);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey && !isTypingTarget(e.target)) {
        e.preventDefault();
        taRef.current?.focus();
      } else if (e.key === "Escape" && streaming) {
        onStop?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [streaming, onStop]);

  const submit = () => {
    const q = value.trim();
    if (!q || streaming || disabled) return;
    onSend(q);
    setValue("");
  };

  const over = value.length > maxLength;
  return (
    <div className="space-y-1.5">
      <div
        className={cn(
          "relative flex items-end gap-2 rounded-[14px] border border-border bg-surface p-2 pl-3.5 shadow-soft transition-[border-color,box-shadow]",
          "focus-within:border-accent focus-within:ring-3 focus-within:ring-accent/15",
          disabled && "opacity-70",
        )}
      >
        <label htmlFor="ask-composer" className="sr-only">
          Ask a question about your data
        </label>
        <textarea
          id="ask-composer"
          ref={taRef}
          rows={1}
          value={value}
          disabled={disabled}
          maxLength={maxLength + 200}
          placeholder={disabled ? (disabledReason ?? "Pick a dataset to start") : (placeholder ?? "Ask a question about your data…")}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            } else if (e.key === "ArrowUp" && !value && lastQuestion) {
              e.preventDefault();
              setValue(lastQuestion);
            }
          }}
          aria-invalid={over || undefined}
          className="scrollbar-thin max-h-[200px] min-h-9 flex-1 resize-none bg-transparent py-1.5 text-sm leading-6 text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
        />
        {streaming ? (
          <Button size="icon-sm" variant="secondary" onClick={onStop} aria-label="Stop answering (Esc)">
            <Square className="fill-current" />
          </Button>
        ) : (
          <Button size="icon-sm" onClick={submit} disabled={!value.trim() || disabled || over} aria-label="Send question (Enter)">
            <ArrowUp />
          </Button>
        )}
      </div>
      <div className="flex items-center justify-between gap-2 px-1 text-[11px] text-fg-subtle">
        <span className="hidden items-center gap-1 sm:flex">
          <Kbd>Enter</Kbd> send · <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> new line · <Kbd>↑</Kbd> last question · <Kbd>/</Kbd> focus
          {streaming && (
            <>
              {" "}
              · <Kbd>Esc</Kbd> stop
            </>
          )}
        </span>
        <span className={cn("tabular ml-auto", over && "text-danger")}>{value.length > maxLength * 0.8 ? `${value.length}/${maxLength}` : ""}</span>
      </div>
    </div>
  );
});
