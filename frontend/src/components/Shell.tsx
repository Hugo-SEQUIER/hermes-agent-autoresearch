"use client";

import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Header />
      <div className="flex h-[calc(100vh-64px)] overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-y-auto bg-surface-container-lowest">
          {children}
        </main>
      </div>
    </>
  );
}
