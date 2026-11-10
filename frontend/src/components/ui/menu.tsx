import * as Menu from "@radix-ui/react-dropdown-menu";
import clsx from "clsx";
import { Icon } from "./Icon";
import { IconButton } from "./primitives";
import { getLang } from "../../lib/i18n";

/* ------------------------------------------------------------------ overflow menu */
export function MoreMenu({ label, items }: { label: string; items: ({ label: string; icon?: string; onSelect: () => void; danger?: boolean } | null | false)[] }) {
  return (
    <Menu.Root dir={getLang() === "ar" ? "rtl" : "ltr"}>
      <Menu.Trigger asChild><IconButton icon="more" label={label} /></Menu.Trigger>
      <Menu.Portal>
        <Menu.Content align="end" sideOffset={4} collisionPadding={8} className="qm-pop z-[90] min-w-48 rounded-md border border-line bg-raised p-1 shadow-raised">
          {items.filter(Boolean).map((it, i) => { const x = it as { label: string; icon?: string; onSelect: () => void; danger?: boolean }; return (
            <Menu.Item key={i} onSelect={x.onSelect} className={clsx("flex cursor-pointer items-center gap-2 rounded-[6px] px-2.5 py-2 text-[13px] outline-none data-[highlighted]:bg-canvas", x.danger ? "text-danger" : "text-fg")}>
              {x.icon && <Icon name={x.icon} size={15} />}{x.label}
            </Menu.Item>
          ); })}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
