(function (exports, vue, shared) {
  'use strict';

  // @__NO_SIDE_EFFECTS__
  function createGenericProjection(fromDomain, toDomain, projector) {
    return (input) => {
      return vue.computed(() => projector(vue.toValue(input), vue.toValue(fromDomain), vue.toValue(toDomain)));
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
    return vue.computed(() => args.every((i) => vue.toValue(i)));
  }

  // @__NO_SIDE_EFFECTS__
  function logicNot(v) {
    return vue.computed(() => !vue.toValue(v));
  }

  // @__NO_SIDE_EFFECTS__
  function logicOr(...args) {
    return vue.computed(() => args.some((i) => vue.toValue(i)));
  }

  // @__NO_SIDE_EFFECTS__
  function useAbs(value) {
    return vue.computed(() => Math.abs(vue.toValue(value)));
  }

  function toValueArgsFlat(args) {
    return args.flatMap((i) => {
      const v = vue.toValue(i);
      if (Array.isArray(v))
        return v.map((i2) => vue.toValue(i2));
      return [v];
    });
  }

  // @__NO_SIDE_EFFECTS__
  function useAverage(...args) {
    return vue.computed(() => {
      const array = toValueArgsFlat(args);
      return array.reduce((sum, v) => sum += v, 0) / array.length;
    });
  }

  // @__NO_SIDE_EFFECTS__
  function useCeil(value) {
    return vue.computed(() => Math.ceil(vue.toValue(value)));
  }

  // @__NO_SIDE_EFFECTS__
  function useClamp(value, min, max) {
    if (typeof value === "function" || vue.isReadonly(value))
      return vue.computed(() => shared.clamp(vue.toValue(value), vue.toValue(min), vue.toValue(max)));
    const _value = vue.ref(value);
    return vue.computed({
      get() {
        return _value.value = shared.clamp(_value.value, vue.toValue(min), vue.toValue(max));
      },
      set(value2) {
        _value.value = shared.clamp(value2, vue.toValue(min), vue.toValue(max));
      }
    });
  }

  // @__NO_SIDE_EFFECTS__
  function useFloor(value) {
    return vue.computed(() => Math.floor(vue.toValue(value)));
  }

  // @__NO_SIDE_EFFECTS__
  function useMath(key, ...args) {
    return shared.reactify(Math[key])(...args);
  }

  // @__NO_SIDE_EFFECTS__
  function useMax(...args) {
    return vue.computed(() => {
      const array = toValueArgsFlat(args);
      return Math.max(...array);
    });
  }

  // @__NO_SIDE_EFFECTS__
  function useMin(...args) {
    return vue.computed(() => {
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
    return vue.computed(() => {
      var _a;
      const _value = vue.toValue(value);
      const _digits = vue.toValue(digits);
      const power = 10 ** _digits;
      return Math[((_a = vue.toValue(options)) == null ? void 0 : _a.math) || "round"](accurateMultiply(_value, power)) / power;
    });
  }

  // @__NO_SIDE_EFFECTS__
  function useProjection(input, fromDomain, toDomain, projector) {
    return createProjection(fromDomain, toDomain, projector)(input);
  }

  // @__NO_SIDE_EFFECTS__
  function useRound(value) {
    return vue.computed(() => Math.round(vue.toValue(value)));
  }

  // @__NO_SIDE_EFFECTS__
  function useSum(...args) {
    return vue.computed(() => toValueArgsFlat(args).reduce((sum, v) => sum += v, 0));
  }

  // @__NO_SIDE_EFFECTS__
  function useTrunc(value) {
    return vue.computed(() => Math.trunc(vue.toValue(value)));
  }

  exports.and = logicAnd;
  exports.createGenericProjection = createGenericProjection;
  exports.createProjection = createProjection;
  exports.logicAnd = logicAnd;
  exports.logicNot = logicNot;
  exports.logicOr = logicOr;
  exports.not = logicNot;
  exports.or = logicOr;
  exports.useAbs = useAbs;
  exports.useAverage = useAverage;
  exports.useCeil = useCeil;
  exports.useClamp = useClamp;
  exports.useFloor = useFloor;
  exports.useMath = useMath;
  exports.useMax = useMax;
  exports.useMin = useMin;
  exports.usePrecision = usePrecision;
  exports.useProjection = useProjection;
  exports.useRound = useRound;
  exports.useSum = useSum;
  exports.useTrunc = useTrunc;

})(this.VueUse = this.VueUse || {}, Vue, VueUse);
