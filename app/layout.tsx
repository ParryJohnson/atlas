import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ATLAS — Algorithmic Trading System",
  description: "Multi-signal confluence trading dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#09090b] text-zinc-100 antialiased">
        {children}
      </body>
    </html>
  );
}
