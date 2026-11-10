import * as CheckboxP from "@radix-ui/react-checkbox";
import clsx from "clsx";
import { Icon } from "./Icon";

export function Checkbox({ checked, onChange, label, className }: { checked: boolean | "indeterminate"; onChange: (v: boolean) => void; label: string; className?: string }) {
  return (
    <CheckboxP.Root checked={checked} onCheckedChange={(v) => onChange(v === true)} aria-label={label}
      className={clsx("grid size-[18px] shrink-0 place-items-center rounded-[5px] border border-line-strong bg-canvas data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=indeterminate]:bg-accent text-accent-ink max-md:size-6", className)}>
      <CheckboxP.Indicator><Icon name={checked === "indeterminate" ? "minus" : "check"} size={13} /></CheckboxP.Indicator>
    </CheckboxP.Root>
  );
}
