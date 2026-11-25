import type { Classification } from "@/lib/types";

export const CLASSIFICATIONS: Array<{ value: Classification; label: string; description: string; aiEligible: boolean }> = [
  {
    value: "internal",
    label: "Internal",
    description: "Default. Business data that stays inside Cortix under synthetic_only.",
    aiEligible: false,
  },
  {
    value: "confidential",
    label: "Confidential",
    description: "Sensitive data. Values are never sent to Gemini.",
    aiEligible: false,
  },
  {
    value: "synthetic",
    label: "Synthetic",
    description: "Generated test data with no real people or records. Requires attestation.",
    aiEligible: true,
  },
  {
    value: "public",
    label: "Public",
    description: "Openly published data. Requires attestation.",
    aiEligible: true,
  },
];

export const needsAttestation = (c: Classification) => c === "synthetic" || c === "public";

export function attestationText(c: Classification) {
  return c === "synthetic"
    ? "I attest this dataset is synthetic: it contains no real personal, customer or confidential data."
    : "I attest this dataset is public: it is openly published and contains no personal or confidential data.";
}
