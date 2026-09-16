import { memo, type RefObject } from "react";

/** Canvas identity is stable across phase, selection and palette updates. */
export const StarCanvasScene = memo(function StarCanvasScene({ canvas, labels, description }: {
  canvas: RefObject<HTMLCanvasElement | null>; labels: RefObject<HTMLCanvasElement | null>; description: string;
}) {
  return <div className="star-canvas-stack">
    <canvas ref={canvas} className="star-surface star-canvas" tabIndex={0} role="img" aria-label={description}>
      请使用下方节点列表访问知识；图谱支持方向键平移、加减键缩放。
    </canvas>
    <canvas ref={labels} className="star-canvas-labels" aria-hidden="true" />
  </div>;
});
