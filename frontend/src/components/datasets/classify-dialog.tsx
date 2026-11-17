"use client";

import * as React from "react";
import { ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Label } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useClassifyDataset } from "@/lib/queries/datasets";
import { errorMessage } from "@/lib/errors";
import type { Classification, Dataset } from "@/lib/types";
import { CLASSIFICATIONS, attestationText, needsAttestation } from "./classification";

export function ClassifyDialog({
  org,
  dataset,
  open,
  onOpenChange,
}: {
  org: string;
  dataset: Dataset;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const [value, setValue] = React.useState<Classification>(dataset.classification);
  const [attested, setAttested] = React.useState(false);
  const classify = useClassifyDataset(org, dataset.id);

  React.useEffect(() => {
    if (open) {
      setValue(dataset.classification);
      setAttested(false);
    }
  }, [open, dataset.classification]);

  const needs = needsAttestation(value);
  const unchanged = value === dataset.classification;

  const submit = async () => {
    try {
      await classify.mutateAsync(value);
      toast.success(`Classified as ${value}`, { description: "The change and your attestation were audited." });
      onOpenChange(false);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader
          icon={<ShieldCheck />}
          title="Change classification"
          description="Classification decides whether this dataset may be sent to Gemini under your AI data policy."
        />
        <DialogBody className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="classify-select">Classification</Label>
            <Select
              value={value}
              onValueChange={(v) => {
                setValue(v as Classification);
                setAttested(false);
              }}
            >
              <SelectTrigger id="classify-select">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CLASSIFICATIONS.map((c) => (
                  <SelectItem key={c.value} value={c.value} description={c.description}>
                    {c.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {needs && (
            <label className="flex cursor-pointer gap-3 rounded-card border border-border bg-surface-2/60 p-3.5 text-sm">
              <Checkbox checked={attested} onCheckedChange={(v) => setAttested(v === true)} className="mt-0.5" />
              <span className="text-fg-muted">{attestationText(value)}</span>
            </label>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={unchanged || (needs && !attested)} loading={classify.isPending}>
            Save classification
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
