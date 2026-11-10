import type { Modifier } from "@dnd-kit/core";
// The two modifiers we need (same behaviour as @dnd-kit/modifiers, without another dependency).
export const restrictToVerticalAxis: Modifier = ({ transform }) => ({ ...transform, x: 0 });
export const restrictToParentElement: Modifier = ({ transform, containerNodeRect, draggingNodeRect }) => {
  if (!containerNodeRect || !draggingNodeRect) return transform;
  const minY = containerNodeRect.top - draggingNodeRect.top, maxY = containerNodeRect.bottom - draggingNodeRect.bottom;
  return { ...transform, y: Math.min(Math.max(transform.y, minY), maxY) };
};
