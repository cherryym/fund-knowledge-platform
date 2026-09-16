import { createStarPhysicsSession } from "./starGraphPhysics";
import type { StarPhysicsCommand, StarPhysicsEvent } from "./starPhysicsProtocol";

// A narrow worker surface avoids importing the conflicting DOM/WebWorker libs
// into the frontend compilation and also permits a real worker_threads adapter.
const scope = self as unknown as {
  onmessage: ((event: { data: StarPhysicsCommand }) => void) | null;
  postMessage(event: StarPhysicsEvent, transfer: ArrayBuffer[]): void;
};
const session = createStarPhysicsSession((event) => {
  scope.postMessage(event, event.type === "frame" ? [event.positions.buffer as ArrayBuffer] : []);
});
scope.onmessage = (event) => session.send(event.data);
