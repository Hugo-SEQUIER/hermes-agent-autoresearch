"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon } from "./Icon";

const NAV_ITEMS = [
  { label: "Dashboard", href: "/" },
  { label: "Research", href: "/runs" },
];

export function Header() {
  const pathname = usePathname();

  return (
    <header className="flex justify-between items-center w-full px-6 h-16 sticky top-0 z-50 bg-surface shadow-[0_32px_32px_rgba(48,80,255,0.08)]">
      <div className="flex items-center gap-8">
        <Link
          href="/"
          className="text-xl font-bold tracking-tighter text-primary font-headline"
        >
          Hermes Observatory
        </Link>
        <nav className="hidden md:flex gap-6">
          {NAV_ITEMS.map((item) => {
            const active =
              item.href === "/"
                ? pathname === "/"
                : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`font-label text-xs uppercase tracking-widest transition-colors duration-200 pb-1 ${
                  active
                    ? "text-primary border-b-2 border-primary-container"
                    : "text-outline-variant hover:text-primary"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
      <div className="flex items-center gap-4">
        <button className="text-on-surface-variant hover:text-primary transition-colors">
          <Icon name="notifications" />
        </button>
        <button className="text-on-surface-variant hover:text-primary transition-colors">
          <Icon name="settings" />
        </button>
      </div>
    </header>
  );
}
