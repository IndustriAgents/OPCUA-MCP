/** Bounded retained data and immediate overload refusal, without a request queue. */
import { CONTRACT } from "./contract.js";
import { ContractRefusal, message } from "./errors.js";
import { AsyncLocalStorage } from "node:async_hooks";

const generations = new AsyncLocalStorage<Map<object, number>>();
export function cacheGeneration<T>(
  caches: ReadonlyArray<{ generation: number }>,
  operation: () => T
): T {
  return generations.run(new Map(caches.map((cache) => [cache, cache.generation])), operation);
}

export class BoundedCache<K, V> extends Map<K, V> {
  private readonly sizes = new Map<K, number>();
  byteCount = 0;
  generation = 0;
  constructor(
    private readonly capacity = CONTRACT.limits.maxCachedNodes,
    private readonly byteLimit = CONTRACT.limits.maxBufferedBytes
  ) {
    super();
  }
  override set(key: K, value: V): this {
    if ((generations.getStore()?.get(this) ?? this.generation) !== this.generation) return this;
    const size = Buffer.byteLength(JSON.stringify([key, value]), "utf8");
    this.delete(key);
    if (size > this.byteLimit) return this;
    super.set(key, value);
    this.sizes.set(key, size);
    this.byteCount += size;
    while (this.size > this.capacity || this.byteCount > this.byteLimit)
      this.delete(this.keys().next().value!);
    return this;
  }
  override delete(key: K): boolean {
    this.byteCount -= this.sizes.get(key) ?? 0;
    this.sizes.delete(key);
    return super.delete(key);
  }
  override clear(): void {
    this.generation++;
    super.clear();
    this.sizes.clear();
    this.byteCount = 0;
  }
}

export class BoundedRecords<T> extends Array<T> {
  static get [Symbol.species]() {
    return Array;
  }
  constructor(
    private readonly countLimit: number,
    private readonly byteLimit: number
  ) {
    super();
  }
  private sizes: number[] = [];
  byteCount = 0;
  override push(...records: T[]): number {
    for (const record of records) {
      const size = Buffer.byteLength(JSON.stringify(record), "utf8");
      if (size > this.byteLimit) continue;
      while (
        this.length &&
        (this.length >= this.countLimit || this.byteCount + size > this.byteLimit)
      )
        this.splice(0, 1);
      super.push(record);
      this.sizes.push(size);
      this.byteCount += size;
    }
    return this.length;
  }
  override splice(start: number, count: number): T[] {
    const removed = super.splice(start, count);
    for (const size of this.sizes.splice(start, count)) this.byteCount -= size;
    return removed;
  }
}

export class Admission {
  private active = 0;
  async run<T>(name: string, operation: () => Promise<T>): Promise<T> {
    const counted = name !== "get_server_status",
      limit = CONTRACT.limits.maxConcurrentCalls;
    if (counted && this.active >= limit)
      throw new ContractRefusal(message("resourceBusy", { resource: "tool calls", limit }));
    if (counted) this.active++;
    try {
      return await operation();
    } finally {
      if (counted) this.active--;
    }
  }
}
