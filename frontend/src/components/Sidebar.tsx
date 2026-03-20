"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon } from "./Icon";

const SIDEBAR_ITEMS = [
  { icon: "smart_toy", label: "Active Runs", href: "/" },
  { icon: "history", label: "Run History", href: "/runs" },
  { icon: "hub", label: "Data Nodes", href: "#" },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden lg:flex flex-col w-64 bg-surface-container-low py-4 shrink-0">
      <div className="px-6 mb-8">
        <div className="flex items-center gap-3 mb-1">
          <Icon name="hub" filled className="text-primary" />
          <span className="text-lg font-semibold text-primary font-headline">
            Research Core
          </span>
        </div>
        <span className="text-[10px] text-outline uppercase tracking-[0.2em] font-label">
          Precision Monitoring
        </span>
      </div>
      <nav className="flex-1 space-y-1">
        {SIDEBAR_ITEMS.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);
          return (
            <Link
              key={item.label}
              href={item.href}
              className={`flex items-center gap-3 px-4 py-3 mx-2 transition-all ${
                active
                  ? "bg-surface-variant text-primary rounded-md translate-x-1"
                  : "text-outline-variant hover:bg-surface-variant/50 hover:text-white"
              }`}
            >
              <Icon name={item.icon} className="text-sm" />
              <span className="font-label text-xs uppercase tracking-widest">
                {item.label}
              </span>
            </Link>
          );
        })}
      </nav>
      <div className="px-4 mt-auto space-y-4">
        <Link
          href="/runs"
          className="block w-full py-3 bg-gradient-to-br from-primary to-primary-container text-on-primary-fixed font-bold text-xs uppercase tracking-widest rounded-md shadow-lg shadow-primary-container/20 active:scale-95 transition-transform text-center"
        >
          New Session
        </Link>
        <div className="pt-4 border-t border-outline-variant/20">
          <a
            href="#"
            className="flex items-center gap-3 px-4 py-2 text-outline-variant hover:text-primary transition-colors"
          >
            <Icon name="sensors" className="text-sm" />
            <span className="font-label text-[10px] uppercase tracking-widest">
              System Status
            </span>
          </a>
        </div>
      </div>
    </aside>
  );
}
