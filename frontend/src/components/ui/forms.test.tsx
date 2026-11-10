import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NumberField, Select, TextField } from "./forms";

afterEach(cleanup);

function SortChoice() {
  const [value, setValue] = useState("default");
  return <Select label="Sort people" value={value} onChange={setValue} options={[
    { value: "default", label: "Default order" },
    { value: "usage", label: "Most used" },
    { value: "disabled", label: "Unavailable", disabled: true },
  ]} />;
}

describe("themed choice menu", () => {
  it("opens by keyboard, selects a value and restores trigger focus", async () => {
    render(<SortChoice />);
    const trigger = screen.getByRole("button", { name: "Sort people" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const choice = await screen.findByRole("menuitemradio", { name: "Most used" });
    expect(screen.getByRole("menuitemradio", { name: "Unavailable" }).getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(choice);
    await waitFor(() => expect(trigger.textContent).toContain("Most used"));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(document.querySelector("select")).toBeNull();
  });

  it("Escape closes without changing the selection", async () => {
    render(<SortChoice />);
    const trigger = screen.getByRole("button", { name: "Sort people" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const menu = await screen.findByRole("menu");
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(trigger.textContent).toContain("Default order");
  });
});

describe("inline field layout", () => {
  it("keeps the label accessible and the action beside the control, above hints", () => {
    const { container } = render(<TextField label="Shared text" labelHidden value="hello" onChange={() => {}}
      hint="Visible helper" action={<button type="submit">Share</button>} />);
    const input = screen.getByRole("textbox", { name: "Shared text" });
    const action = screen.getByRole("button", { name: "Share" });
    expect(container.querySelector("label")?.classList.contains("qm-sr")).toBe(true);
    expect(input.parentElement?.parentElement).toBe(action.parentElement);
    expect(screen.getByText("Visible helper").parentElement).toBe(action.parentElement?.parentElement);
    expect(input.getAttribute("aria-describedby")).toBe(screen.getByText("Visible helper").id);
  });

  it("keeps long units in a separate flex item so they cannot cover numbers", () => {
    render(<NumberField label="Line speed" value={1000} onValue={() => {}} unit="megabits per second" />);
    const input = screen.getByRole("textbox", { name: "Line speed" });
    const unit = screen.getByText("megabits per second");
    expect(unit.parentElement).toBe(input.parentElement);
    expect(unit.className).not.toContain("absolute");
    fireEvent.change(input, { target: { value: "1200" } });
    expect(input.getAttribute("value")).toBe("1200");
  });
});
