"use client";

export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100dvh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: "system-ui, sans-serif",
          background: "#fbfbfc",
          color: "#0e1116",
        }}
      >
        <div style={{ textAlign: "center", padding: 24 }}>
          <h1 style={{ fontSize: 20, fontWeight: 600 }}>NEXORA hit an unexpected error</h1>
          <p style={{ color: "#5b6270", fontSize: 14 }}>Reload the page to continue.</p>
          <button
            onClick={reset}
            style={{
              marginTop: 12,
              height: 36,
              padding: "0 14px",
              borderRadius: 6,
              border: 0,
              background: "#4f46e5",
              color: "#fff",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
