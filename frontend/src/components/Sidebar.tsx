"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon } from "./Icon";

const SIDEBAR_ITEMS = [
  { icon: "chat", label: "Dashboard", href: "/" },
  { icon: "history", label: "All Runs", href: "/runs" },
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
    </aside>
  );
}
