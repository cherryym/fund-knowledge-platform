/**
 * Same-page coordination only; no React, DOM, timers or persistent storage.
 * Each graph creates one empty object token and releases it in its disposer:
 *   const owner = {};
 *   setGraphInteraction(owner, true);  // gesture begins (repeated calls are safe)
 *   setGraphInteraction(owner, false); // gesture ends AND on graph disposal
 * Never attach DOM, business content or user data to a token.
 */
const owners = new Set<object>();
type Subscription = {
  callback: (active: boolean) => void;
  lastActive: boolean;
};
const subscriptions = new Set<Subscription>();
let notifying = false;
let notificationPending = false;

/** Read the aggregate state, including interactions started before subscribing. */
export function isGraphInteracting(): boolean {
  return owners.size > 0;
}

function notify() {
  notificationPending = true;
  if (notifying) return;
  notifying = true;
  try {
    // A listener may release an owner or unsubscribe while being notified.
    // Deliver current state, never a stale `true` after a nested final release.
    while (notificationPending) {
      notificationPending = false;
      for (const subscription of [...subscriptions]) {
        const active = isGraphInteracting();
        if (!subscriptions.has(subscription) || subscription.lastActive === active)
          continue;
        subscription.lastActive = active;
        subscription.callback(active);
      }
    }
  } finally {
    notifying = false;
  }
}

/** A token is an identity, not a reference to a graph, element or data record. */
export function setGraphInteraction(owner: object, active: boolean): void {
  const wasActive = isGraphInteracting();
  if (active) {
    if (owners.has(owner)) return;
    if (
      owner === null || typeof owner !== "object" ||
      (Object.getPrototypeOf(owner) !== Object.prototype && Object.getPrototypeOf(owner) !== null) ||
      Reflect.ownKeys(owner).length !== 0
    )
      throw new TypeError("Graph interaction owner must be an empty object token.");
    owners.add(owner);
  } else {
    if (!owners.delete(owner)) return;
  }
  if (wasActive !== isGraphInteracting()) notify();
}

/**
 * Change notifications only (no immediate callback); read isGraphInteracting()
 * when mounting. The returned cleanup is independent and idempotent. A graph's
 * token must also be released by its own disposer; unsubscribing is not a release.
 */
export function subscribeGraphInteraction(callback: (active: boolean) => void): () => void {
  const subscription = { callback, lastActive: isGraphInteracting() };
  subscriptions.add(subscription);
  return () => {
    subscriptions.delete(subscription);
  };
}
