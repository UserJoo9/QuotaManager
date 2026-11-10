// Drag-and-drop for the sidebar. Loaded on first interaction with the sidebar (not on first paint).
import type { CSSProperties, ReactNode } from "react";
import { DndContext, KeyboardSensor, PointerSensor, TouchSensor, closestCenter, useSensor, useSensors, type DragEndEvent, type Announcements, type DraggableAttributes, type DraggableSyntheticListeners } from "@dnd-kit/core";
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { restrictToVerticalAxis, restrictToParentElement } from "./dndModifiers";
import { t, tf } from "../lib/i18n";

export interface SortProps { setNodeRef: (el: HTMLElement | null) => void; style: CSSProperties; handle: DraggableAttributes & DraggableSyntheticListeners; isDragging: boolean; isOver: boolean }
export function SortableGroup({ ids, onReorder, labelOf, children }: { ids: string[]; onReorder: (next: string[]) => void; labelOf: (id: string) => string; children: (id: string, s: SortProps) => ReactNode }) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 6 } }), // long-press on touch
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const announcements: Announcements = {
    onDragStart: ({ active }) => tf("nav.dnd.start", { name: labelOf(String(active.id)) }),
    onDragOver: ({ active, over }) => (over ? tf("nav.dnd.over", { name: labelOf(String(active.id)), target: labelOf(String(over.id)) }) : ""),
    onDragEnd: ({ active, over }) => (over ? tf("nav.dnd.end", { name: labelOf(String(active.id)), target: labelOf(String(over.id)) }) : tf("nav.dnd.cancel", { name: labelOf(String(active.id)) })),
    onDragCancel: ({ active }) => tf("nav.dnd.cancel", { name: labelOf(String(active.id)) }),
  };
  const onDragEnd = (e: DragEndEvent) => {
    if (!e.over || e.active.id === e.over.id) return;
    onReorder(arrayMove(ids, ids.indexOf(String(e.active.id)), ids.indexOf(String(e.over.id))));
  };
  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd} modifiers={[restrictToVerticalAxis, restrictToParentElement]}
      accessibility={{ announcements, screenReaderInstructions: { draggable: t("nav.dnd.help") } }}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {ids.map((id) => <Item key={id} id={id}>{(s) => children(id, s)}</Item>)}
      </SortableContext>
    </DndContext>
  );
}
function Item({ id, children }: { id: string; children: (s: SortProps) => ReactNode }) {
  const s = useSortable({ id });
  return <>{children({ setNodeRef: s.setNodeRef, style: { transform: CSS.Translate.toString(s.transform), transition: s.transition }, handle: { ...s.attributes, ...s.listeners } as SortProps["handle"], isDragging: s.isDragging, isOver: s.isOver })}</>;
}
