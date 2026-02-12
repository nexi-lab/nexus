import { computed, toValue, isReadonly, ref } from 'vue';
import { clamp, reactify } from '@vueuse/shared';

// @__NO_SIDE_EFFECTS__
function createGenericProjection(fromDomain, toDomain, projector) {
  return (input) => {
    return computed(() => projector(toValue(input), toValue(fromDomain), toValue(toDomain)));
  };
}

function defaultNumericProjector(input, from, to) {
  return (input - from[0]) / (from[1] - from[0]) * (to[1] - to[0]) + to[0];
}
// @__NO_SIDE_EFFECTS__
function createProjection(fromDomain, toDomain, projector = defaultNumericProjector) {
  return createGenericProjection(fromDomain, toDomain, projector);
}

// @__NO_SIDE_EFFECTS__
function logicAnd(...args) {
  return computed(() => args.every((i) => toValue(i)));
}

// @__NO_SIDE_EFFECTS__
function logicNot(v) {
  return computed(() => !toValue(v));
}

// @__NO_SIDE_EFFECTS__
function logicOr(...args) {
  return computed(() => args.some((i) => toValue(i)));
}

// @__NO_SIDE_EFFECTS__
function useAbs(value) {
  return computed(() => Math.abs(toValue(value)));
}

function toValueArgsFlat(args) {
  return args.flatMap((i) => {
    const v = toValue(i);
    if (Array.isArray(v))
      return v.map((i2) => toValue(i2));
    return [v];
  });
}

// @__NO_SIDE_EFFECTS__
function useAverage(...args) {
  return computed(() => {
    const array = toValueArgsFlat(args);
    return array.reduce((sum, v) => sum += v, 0) / array.length;
  });
}

// @__NO_SIDE_EFFECTS__
function useCeil(value) {
  return computed(() => Math.ceil(toValue(value)));
}

// @__NO_SIDE_EFFECTS__
function useClamp(value, min, max) {
  if (typeof value === "function" || isReadonly(value))
    return computed(() => clamp(toValue(value), toValue(min), toValue(max)));
  const _value = ref(value);
  return computed({
    get() {
      return _value.value = clamp(_value.value, toValue(min), toValue(max));
    },
    set(value2) {
      _value.value = clamp(value2, toValue(min), toValue(max));
    }
  });
}

// @__NO_SIDE_EFFECTS__
function useFloor(value) {
  return computed(() => Math.floor(toValue(value)));
}

// @__NO_SIDE_EFFECTS__
function useMath(key, ...args) {
  return reactify(Math[key])(...args);
}

// @__NO_SIDE_EFFECTS__
function useMax(...args) {
  return computed(() => {
    const array = toValueArgsFlat(args);
    return Math.max(...array);
  });
}

// @__NO_SIDE_EFFECTS__
function useMin(...args) {
  return computed(() => {
    const array = toValueArgsFlat(args);
    return Math.min(...array);
  });
}

function accurateMultiply(value, power) {
  const valueStr = value.toString();
  if (value > 0 && valueStr.includes(".")) {
    const decimalPlaces = valueStr.split(".")[1].length;
    const multiplier = 10 ** decimalPlaces;
    return value * multiplier * power / multiplier;
  } else {
    return value * power;
  }
}
// @__NO_SIDE_EFFECTS__
function usePrecision(value, digits, options) {
  return computed(() => {
    var _a;
    const _value = toValue(value);
    const _digits = toValue(digits);
    const power = 10 ** _digits;
    return Math[((_a = toValue(options)) == null ? void 0 : _a.math) || "round"](accurateMultiply(_value, power)) / power;
  });
}

// @__NO_SIDE_EFFECTS__
function useProjection(input, fromDomain, toDomain, projector) {
  return createProjection(fromDomain, toDomain, projector)(input);
}

// @__NO_SIDE_EFFECTS__
function useRound(value) {
  return computed(() => Math.round(toValue(value)));
}

// @__NO_SIDE_EFFECTS__
function useSum(...args) {
  return computed(() => toValueArgsFlat(args).reduce((sum, v) => sum += v, 0));
}

// @__NO_SIDE_EFFECTS__
function useTrunc(value) {
  return computed(() => Math.trunc(toValue(value)));
}

export { logicAnd as and, createGenericProjection, createProjection, logicAnd, logicNot, logicOr, logicNot as not, logicOr as or, useAbs, useAverage, useCeil, useClamp, useFloor, useMath, useMax, useMin, usePrecision, useProjection, useRound, useSum, useTrunc };
