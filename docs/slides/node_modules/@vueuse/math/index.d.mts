import * as vue from 'vue';
import { MaybeRefOrGetter, ComputedRef, Ref } from 'vue';
import { ReadonlyRefOrGetter, ArgumentsType, Reactified } from '@vueuse/shared';

type ProjectorFunction<F, T> = (input: F, from: readonly [F, F], to: readonly [T, T]) => T;
type UseProjection<F, T> = (input: MaybeRefOrGetter<F>) => ComputedRef<T>;
declare function createGenericProjection<F = number, T = number>(fromDomain: MaybeRefOrGetter<readonly [F, F]>, toDomain: MaybeRefOrGetter<readonly [T, T]>, projector: ProjectorFunction<F, T>): UseProjection<F, T>;

declare function createProjection(fromDomain: MaybeRefOrGetter<readonly [number, number]>, toDomain: MaybeRefOrGetter<readonly [number, number]>, projector?: ProjectorFunction<number, number>): UseProjection<number, number>;

/**
 * `AND` conditions for refs.
 *
 * @see https://vueuse.org/logicAnd
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function logicAnd(...args: MaybeRefOrGetter<any>[]): ComputedRef<boolean>;

/**
 * `NOT` conditions for refs.
 *
 * @see https://vueuse.org/logicNot
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function logicNot(v: MaybeRefOrGetter<any>): ComputedRef<boolean>;

/**
 * `OR` conditions for refs.
 *
 * @see https://vueuse.org/logicOr
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function logicOr(...args: MaybeRefOrGetter<any>[]): ComputedRef<boolean>;

/**
 * Reactive `Math.abs`.
 *
 * @see https://vueuse.org/useAbs
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useAbs(value: MaybeRefOrGetter<number>): ComputedRef<number>;

declare function useAverage(array: MaybeRefOrGetter<MaybeRefOrGetter<number>[]>): ComputedRef<number>;
declare function useAverage(...args: MaybeRefOrGetter<number>[]): ComputedRef<number>;

/**
 * Reactive `Math.ceil`.
 *
 * @see https://vueuse.org/useCeil
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useCeil(value: MaybeRefOrGetter<number>): ComputedRef<number>;

/**
 * Reactively clamp a value between two other values.
 *
 * @see https://vueuse.org/useClamp
 * @param value number
 * @param min
 * @param max
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useClamp(value: ReadonlyRefOrGetter<number>, min: MaybeRefOrGetter<number>, max: MaybeRefOrGetter<number>): ComputedRef<number>;
declare function useClamp(value: MaybeRefOrGetter<number>, min: MaybeRefOrGetter<number>, max: MaybeRefOrGetter<number>): Ref<number>;

/**
 * Reactive `Math.floor`
 *
 * @see https://vueuse.org/useFloor
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useFloor(value: MaybeRefOrGetter<number>): ComputedRef<number>;

type UseMathKeys = keyof {
    [K in keyof Math as Math[K] extends (...args: any) => any ? K : never]: unknown;
};
/**
 * Reactive `Math` methods.
 *
 * @see https://vueuse.org/useMath
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useMath<K extends keyof Math>(key: K, ...args: ArgumentsType<Reactified<Math[K], true>>): ReturnType<Reactified<Math[K], true>>;

declare function useMax(array: MaybeRefOrGetter<MaybeRefOrGetter<number>[]>): ComputedRef<number>;
declare function useMax(...args: MaybeRefOrGetter<number>[]): ComputedRef<number>;

declare function useMin(array: MaybeRefOrGetter<MaybeRefOrGetter<number>[]>): ComputedRef<number>;
declare function useMin(...args: MaybeRefOrGetter<number>[]): ComputedRef<number>;

interface UsePrecisionOptions {
    /**
     * Method to use for rounding
     *
     * @default 'round'
     */
    math?: 'floor' | 'ceil' | 'round';
}
/**
 * Reactively set the precision of a number.
 *
 * @see https://vueuse.org/usePrecision
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function usePrecision(value: MaybeRefOrGetter<number>, digits: MaybeRefOrGetter<number>, options?: MaybeRefOrGetter<UsePrecisionOptions>): ComputedRef<number>;

/**
 * Reactive numeric projection from one domain to another.
 *
 * @see https://vueuse.org/useProjection
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useProjection(input: MaybeRefOrGetter<number>, fromDomain: MaybeRefOrGetter<readonly [number, number]>, toDomain: MaybeRefOrGetter<readonly [number, number]>, projector?: ProjectorFunction<number, number>): vue.ComputedRef<number>;

/**
 * Reactive `Math.round`.
 *
 * @see https://vueuse.org/useRound
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useRound(value: MaybeRefOrGetter<number>): ComputedRef<number>;

declare function useSum(array: MaybeRefOrGetter<MaybeRefOrGetter<number>[]>): ComputedRef<number>;
declare function useSum(...args: MaybeRefOrGetter<number>[]): ComputedRef<number>;

/**
 * Reactive `Math.trunc`.
 *
 * @see https://vueuse.org/useTrunc
 *
 * @__NO_SIDE_EFFECTS__
 */
declare function useTrunc(value: MaybeRefOrGetter<number>): ComputedRef<number>;

export { logicAnd as and, createGenericProjection, createProjection, logicAnd, logicNot, logicOr, logicNot as not, logicOr as or, useAbs, useAverage, useCeil, useClamp, useFloor, useMath, useMax, useMin, usePrecision, useProjection, useRound, useSum, useTrunc };
export type { ProjectorFunction, UseMathKeys, UsePrecisionOptions, UseProjection };
