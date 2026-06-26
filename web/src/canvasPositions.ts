export interface FlowPositionProjector {
  screenToFlowPosition(position: { x: number; y: number }): { x: number; y: number };
}

export interface CanvasBoundsElement {
  getBoundingClientRect(): {
    left: number;
    top: number;
    width: number;
    height: number;
  };
}

const dagNodeHalfWidth = 96;
const dagNodeHalfHeight = 32;

export function canvasCenterNodePosition(
  flowInstance: FlowPositionProjector | null,
  canvasElement: CanvasBoundsElement | null,
): { x: number; y: number } {
  if (!canvasElement) return { x: 0, y: 0 };
  const bounds = canvasElement.getBoundingClientRect();
  const screenCenter = {
    x: bounds.left + bounds.width / 2,
    y: bounds.top + bounds.height / 2,
  };
  const center = flowInstance
    ? flowInstance.screenToFlowPosition(screenCenter)
    : { x: bounds.width / 2, y: bounds.height / 2 };
  return {
    x: Math.round(center.x - dagNodeHalfWidth),
    y: Math.round(center.y - dagNodeHalfHeight),
  };
}
