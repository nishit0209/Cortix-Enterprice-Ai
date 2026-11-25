import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Logo, LogoMark } from "./logo";

describe("Cortix Logo and LogoMark branding components", () => {
  it("renders LogoMark as an SVG with aria-hidden and default size", () => {
    const { container } = render(<LogoMark />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("width", "28");
    expect(svg).toHaveAttribute("height", "28");
    expect(svg).toHaveAttribute("viewBox", "0 0 32 32");
  });

  it("renders LogoMark with a custom size and className", () => {
    const { container } = render(<LogoMark size={36} className="custom-mark-class" />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("width", "36");
    expect(svg).toHaveAttribute("height", "36");
    expect(svg?.getAttribute("class")).toContain("custom-mark-class");
  });

  it("renders full Logo with wordmark 'CORTIX' when not collapsed", () => {
    render(<Logo />);
    const wordmark = screen.getByText("CORTIX");
    expect(wordmark).toBeInTheDocument();
    expect(wordmark).toHaveClass("tracking-[0.14em]");
    expect(wordmark.textContent).toBe("CORTIX");
  });

  it("hides the wordmark when collapsed is true", () => {
    render(<Logo collapsed />);
    expect(screen.queryByText("CORTIX")).toBeNull();
  });

  it("supports passing custom size to Logo", () => {
    const { container } = render(<Logo size={32} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("width", "32");
    expect(svg).toHaveAttribute("height", "32");
    expect(screen.getByText("CORTIX")).toBeInTheDocument();
  });

  it("does not render any legacy NEXORA branding text", () => {
    const { container } = render(<Logo />);
    expect(container.textContent).not.toContain("NEXORA");
    expect(container.innerHTML).not.toContain("NEXORA");
  });
});
