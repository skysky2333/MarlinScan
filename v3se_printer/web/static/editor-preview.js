"use strict";

const SOURCE_NAMES = new Set(["full", "local"]);
const IDENTITY_TONE_CURVE = Object.freeze([0, 0.25, 0.5, 0.75, 1]);
const RECIPE_FIELDS = Object.freeze([
  "version",
  "material",
  "exposure_ev",
  "temperature",
  "tint",
  "contrast",
  "highlights",
  "shadows",
  "black_point",
  "white_point",
  "saturation",
  "red_balance",
  "green_balance",
  "blue_balance",
  "film_base_red",
  "film_base_green",
  "film_base_blue",
  "film_density",
  "film_dmin",
  "film_dmax",
  "film_red_ratio",
  "film_blue_ratio",
  "slide_fade",
  "slide_black_red",
  "slide_black_green",
  "slide_black_blue",
  "slide_white_red",
  "slide_white_green",
  "slide_white_blue",
  "tone_curve",
  "hsl_hue",
  "hsl_saturation",
  "hsl_lightness",
]);
const DEFAULT_RECIPE = Object.freeze({
  version: 2,
  material: "positive",
  exposure_ev: 0,
  temperature: 0,
  tint: 0,
  contrast: 0,
  highlights: 0,
  shadows: 0,
  black_point: 0,
  white_point: 1,
  saturation: 1,
  red_balance: 1,
  green_balance: 1,
  blue_balance: 1,
  film_base_red: 1,
  film_base_green: 1,
  film_base_blue: 1,
  film_density: 1,
  film_dmin: 0,
  film_dmax: 4,
  film_red_ratio: 1,
  film_blue_ratio: 1,
  slide_fade: 0,
  slide_black_red: 0,
  slide_black_green: 0,
  slide_black_blue: 0,
  slide_white_red: 1,
  slide_white_green: 1,
  slide_white_blue: 1,
  tone_curve: IDENTITY_TONE_CURVE,
  hsl_hue: Object.freeze([0, 0, 0, 0, 0, 0, 0, 0]),
  hsl_saturation: Object.freeze([0, 0, 0, 0, 0, 0, 0, 0]),
  hsl_lightness: Object.freeze([0, 0, 0, 0, 0, 0, 0, 0]),
});

const VERTEX_SHADER = `#version 300 es
precision highp float;
out vec2 v_uv;

void main() {
  vec2 position = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  v_uv = position;
  gl_Position = vec4(position * 2.0 - 1.0, 0.0, 1.0);
}
`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp int;

const mat3 SRGB_TO_REC2020 = mat3(
  0.627452, 0.069109, 0.016398,
  0.329249, 0.919531, 0.088030,
  0.043299, 0.011360, 0.895572
);
const mat3 REC2020_TO_SRGB = mat3(
  1.660491, -0.124550, -0.018151,
  -0.587641, 1.132900, -0.100579,
  -0.072850, -0.008349, 1.118730
);
const vec3 REC2020_LUMINANCE = vec3(0.2627, 0.6780, 0.0593);

uniform sampler2D u_image;
uniform vec2 u_uv_origin;
uniform vec2 u_uv_scale;
uniform int u_output_mode;
uniform int u_material;
uniform float u_exposure_ev;
uniform float u_temperature;
uniform float u_tint;
uniform float u_contrast;
uniform float u_highlights;
uniform float u_shadows;
uniform float u_black_point;
uniform float u_white_point;
uniform float u_saturation;
uniform vec3 u_rgb_balance;
uniform vec3 u_film_base;
uniform float u_film_density;
uniform float u_film_dmin;
uniform float u_film_dmax;
uniform float u_film_red_ratio;
uniform float u_film_blue_ratio;
uniform float u_slide_fade;
uniform vec3 u_slide_black;
uniform vec3 u_slide_white;
uniform int u_tone_active;
uniform float u_tone_curve[5];
uniform int u_hsl_active;
uniform float u_hsl_hue[8];
uniform float u_hsl_saturation[8];
uniform float u_hsl_lightness[8];

in vec2 v_uv;
out vec4 output_color;

vec3 decodeSrgb(vec3 encoded) {
  bvec3 high = greaterThan(encoded, vec3(0.04045));
  vec3 lowResult = encoded / 12.92;
  vec3 highResult = pow(max((encoded + 0.055) / 1.055, vec3(0.0)), vec3(2.4));
  return mix(lowResult, highResult, high);
}

vec3 encodeSrgb(vec3 linear) {
  bvec3 high = greaterThan(linear, vec3(0.0031308));
  vec3 lowResult = linear * 12.92;
  vec3 highResult = 1.055 * pow(max(linear, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
  return mix(lowResult, highResult, high);
}

float hueToRgb(float p, float q, float hue) {
  float wrapped = fract(hue);
  if (wrapped < 1.0 / 6.0) return p + (q - p) * 6.0 * wrapped;
  if (wrapped < 0.5) return q;
  if (wrapped < 2.0 / 3.0) return p + (q - p) * (2.0 / 3.0 - wrapped) * 6.0;
  return p;
}

vec3 rgbToHsl(vec3 rgb) {
  float maximum = max(max(rgb.r, rgb.g), rgb.b);
  float minimum = min(min(rgb.r, rgb.g), rgb.b);
  float delta = maximum - minimum;
  float lightness = (maximum + minimum) * 0.5;
  if (delta <= 1e-7) return vec3(0.0, 0.0, lightness);
  float saturation = lightness < 0.5
    ? delta / (maximum + minimum)
    : delta / (2.0 - maximum - minimum);
  float hue;
  if (maximum == rgb.r) {
    hue = (rgb.g - rgb.b) / delta + (rgb.g < rgb.b ? 6.0 : 0.0);
  } else if (maximum == rgb.g) {
    hue = (rgb.b - rgb.r) / delta + 2.0;
  } else {
    hue = (rgb.r - rgb.g) / delta + 4.0;
  }
  return vec3(hue * 60.0, saturation, lightness);
}

vec3 hslToRgb(vec3 hsl) {
  if (hsl.y <= 1e-7) return vec3(hsl.z);
  float hue = hsl.x / 360.0;
  float q = hsl.z < 0.5 ? hsl.z * (1.0 + hsl.y) : hsl.z + hsl.y - hsl.z * hsl.y;
  float p = 2.0 * hsl.z - q;
  return vec3(
    hueToRgb(p, q, hue + 1.0 / 3.0),
    hueToRgb(p, q, hue),
    hueToRgb(p, q, hue - 1.0 / 3.0)
  );
}

float interpolateControl(float controls[8], float hue) {
  float position = hue / 45.0;
  float lowerValue = floor(position);
  int lower = int(lowerValue) % 8;
  int upper = (lower + 1) % 8;
  return mix(controls[lower], controls[upper], position - lowerValue);
}

float adjustUnitAxis(float value, float adjustment) {
  return adjustment < 0.0
    ? value * (1.0 + adjustment)
    : value + (1.0 - value) * adjustment;
}

float mapTone(float value) {
  float position = clamp(value, 0.0, 1.0) * 4.0;
  int lower = min(int(floor(position)), 3);
  return mix(u_tone_curve[lower], u_tone_curve[lower + 1], position - float(lower));
}

vec3 convertMaterial(vec3 rgb) {
  if (u_material == 0) {
    if (u_slide_fade == 0.0) return rgb;
    vec3 normalized = (rgb - u_slide_black) / (u_slide_white - u_slide_black);
    return rgb + u_slide_fade * (normalized - rgb);
  }
  vec3 density = log2(max(rgb / u_film_base, vec3(exp2(-16.0))));
  density *= vec3(
    -u_film_density * u_film_red_ratio,
    -u_film_density,
    -u_film_density * u_film_blue_ratio
  );
  density = (density - u_film_dmin) / (u_film_dmax - u_film_dmin);
  if (u_material == 2) density = vec3(dot(density, REC2020_LUMINANCE));
  return density;
}

vec3 applyHsl(vec3 rgb) {
  vec3 encoded = encodeSrgb(REC2020_TO_SRGB * rgb);
  vec3 bounded = clamp(encoded, 0.0, 1.0);
  vec3 hsl = rgbToHsl(bounded);
  float hueAdjustment = interpolateControl(u_hsl_hue, hsl.x);
  float saturationAdjustment = interpolateControl(u_hsl_saturation, hsl.x);
  float lightnessAdjustment = interpolateControl(u_hsl_lightness, hsl.x);
  hsl.x = mod(hsl.x + hueAdjustment + 360.0, 360.0);
  hsl.y = adjustUnitAxis(hsl.y, saturationAdjustment);
  hsl.z = adjustUnitAxis(hsl.z, lightnessAdjustment);
  encoded += hslToRgb(hsl) - bounded;
  return SRGB_TO_REC2020 * decodeSrgb(encoded);
}

vec3 applyRecipe(vec3 rgb) {
  vec3 result = convertMaterial(rgb);
  float temperature = exp2(u_temperature);
  float tint = exp2(u_tint);
  result *= u_rgb_balance * vec3(temperature * sqrt(tint), 1.0 / tint, sqrt(tint) / temperature);
  result *= exp2(u_exposure_ev);
  result = (result - u_black_point) / (u_white_point - u_black_point);
  vec3 unit = clamp(result, 0.0, 1.0);
  result += u_shadows * 0.25 * (1.0 - unit) * (1.0 - unit);
  result += u_highlights * 0.25 * unit * unit;
  result = (result - 0.18) * exp2(u_contrast) + 0.18;
  float luminance = dot(result, REC2020_LUMINANCE);
  result = vec3(luminance) + (result - luminance) * u_saturation;
  if (u_tone_active == 1) {
    float bounded = clamp(dot(result, REC2020_LUMINANCE), 0.0, 1.0);
    result += mapTone(bounded) - bounded;
  }
  if (u_hsl_active == 1) result = applyHsl(result);
  return result;
}

void main() {
  vec2 uv = u_uv_origin + v_uv * u_uv_scale;
  vec3 source = SRGB_TO_REC2020 * decodeSrgb(texture(u_image, uv).rgb);
  if (u_output_mode == 1) {
    output_color = vec4(source, 1.0);
    return;
  }
  vec3 linearSrgb = clamp(REC2020_TO_SRGB * applyRecipe(source), 0.0, 1.0);
  output_color = vec4(encodeSrgb(linearSrgb), 1.0);
}
`;

function validateSourceName(source) {
  if (!SOURCE_NAMES.has(source)) throw new TypeError("Editor preview source must be full or local");
}

function validateNumber(recipe, name, minimum, maximum) {
  const value = recipe[name];
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new TypeError(`Editor recipe ${name} must be from ${minimum} through ${maximum}`);
  }
}

function validateArray(recipe, name, length, minimum, maximum) {
  const values = recipe[name];
  if (!Array.isArray(values) || values.length !== length) {
    throw new TypeError(`Editor recipe ${name} must contain ${length} values`);
  }
  values.forEach((value) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
      throw new TypeError(`Editor recipe ${name} values must be from ${minimum} through ${maximum}`);
    }
  });
}

function validateRecipe(recipe) {
  if (recipe === null || typeof recipe !== "object" || Array.isArray(recipe)) {
    throw new TypeError("Editor recipe must be an object");
  }
  const keys = Object.keys(recipe);
  if (keys.length !== RECIPE_FIELDS.length || RECIPE_FIELDS.some((name) => !Object.hasOwn(recipe, name))) {
    throw new TypeError("Editor recipe fields do not match version 2");
  }
  if (recipe.version !== 2) throw new TypeError("Editor recipe version must be 2");
  if (!["positive", "color_negative", "bw_negative"].includes(recipe.material)) {
    throw new TypeError("Editor recipe material is invalid");
  }
  [
    ["exposure_ev", -8, 8],
    ["temperature", -1, 1],
    ["tint", -1, 1],
    ["contrast", -1, 1],
    ["highlights", -1, 1],
    ["shadows", -1, 1],
    ["black_point", -1, 0.95],
    ["white_point", 0.01, 8],
    ["saturation", 0, 3],
    ["red_balance", 0.1, 4],
    ["green_balance", 0.1, 4],
    ["blue_balance", 0.1, 4],
    ["film_base_red", 0.01, 4],
    ["film_base_green", 0.01, 4],
    ["film_base_blue", 0.01, 4],
    ["film_density", 0.1, 4],
    ["film_dmin", -4, 4],
    ["film_dmax", -4, 8],
    ["film_red_ratio", 0.1, 4],
    ["film_blue_ratio", 0.1, 4],
    ["slide_fade", 0, 1],
  ].forEach(([name, minimum, maximum]) => validateNumber(recipe, name, minimum, maximum));
  [
    "slide_black_red",
    "slide_black_green",
    "slide_black_blue",
    "slide_white_red",
    "slide_white_green",
    "slide_white_blue",
  ].forEach((name) => {
    if (typeof recipe[name] !== "number" || !Number.isFinite(recipe[name])) {
      throw new TypeError(`Editor recipe ${name} must be finite`);
    }
  });
  if (recipe.black_point >= recipe.white_point) throw new RangeError("Editor recipe black point must be below white point");
  if (recipe.film_dmin >= recipe.film_dmax) throw new RangeError("Editor recipe film dmin must be below film dmax");
  ["red", "green", "blue"].forEach((channel) => {
    if (recipe[`slide_black_${channel}`] >= recipe[`slide_white_${channel}`]) {
      throw new RangeError("Editor recipe slide black points must be below slide white points");
    }
  });
  validateArray(recipe, "tone_curve", 5, 0, 1);
  validateArray(recipe, "hsl_hue", 8, -30, 30);
  validateArray(recipe, "hsl_saturation", 8, -1, 1);
  validateArray(recipe, "hsl_lightness", 8, -1, 1);
  return Object.freeze({
    ...recipe,
    tone_curve: Object.freeze([...recipe.tone_curve]),
    hsl_hue: Object.freeze([...recipe.hsl_hue]),
    hsl_saturation: Object.freeze([...recipe.hsl_saturation]),
    hsl_lightness: Object.freeze([...recipe.hsl_lightness]),
  });
}

function compileShader(gl, type, source, label) {
  const shader = gl.createShader(type);
  if (shader === null) throw new Error(`WebGL2 could not allocate the ${label} shader`);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) || "unknown compiler error";
    gl.deleteShader(shader);
    throw new Error(`Editor preview ${label} shader failed to compile: ${log}`);
  }
  return shader;
}

function createProgram(gl) {
  const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER, "vertex");
  const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER, "fragment");
  const program = gl.createProgram();
  if (program === null) throw new Error("WebGL2 could not allocate the editor preview program");
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.detachShader(program, vertex);
  gl.detachShader(program, fragment);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(program) || "unknown linker error";
    gl.deleteProgram(program);
    throw new Error(`Editor preview shader program failed to link: ${log}`);
  }
  return program;
}

function uniformLocations(gl, program) {
  const names = [
    "u_image",
    "u_uv_origin",
    "u_uv_scale",
    "u_output_mode",
    "u_material",
    "u_exposure_ev",
    "u_temperature",
    "u_tint",
    "u_contrast",
    "u_highlights",
    "u_shadows",
    "u_black_point",
    "u_white_point",
    "u_saturation",
    "u_rgb_balance",
    "u_film_base",
    "u_film_density",
    "u_film_dmin",
    "u_film_dmax",
    "u_film_red_ratio",
    "u_film_blue_ratio",
    "u_slide_fade",
    "u_slide_black",
    "u_slide_white",
    "u_tone_active",
    "u_tone_curve[0]",
    "u_hsl_active",
    "u_hsl_hue[0]",
    "u_hsl_saturation[0]",
    "u_hsl_lightness[0]",
  ];
  return Object.fromEntries(names.map((name) => {
    const location = gl.getUniformLocation(program, name);
    if (location === null) throw new Error(`Editor preview shader is missing uniform ${name}`);
    return [name, location];
  }));
}

function glErrorName(gl, error) {
  const names = new Map([
    [gl.INVALID_ENUM, "INVALID_ENUM"],
    [gl.INVALID_VALUE, "INVALID_VALUE"],
    [gl.INVALID_OPERATION, "INVALID_OPERATION"],
    [gl.INVALID_FRAMEBUFFER_OPERATION, "INVALID_FRAMEBUFFER_OPERATION"],
    [gl.OUT_OF_MEMORY, "OUT_OF_MEMORY"],
  ]);
  return names.get(error) || `0x${error.toString(16)}`;
}

export class EditorPreviewRenderer {
  constructor(canvas) {
    if (canvas === null || typeof canvas !== "object" || typeof canvas.getContext !== "function") {
      throw new TypeError("EditorPreviewRenderer requires a canvas");
    }
    const gl = canvas.getContext("webgl2", {
      alpha: false,
      antialias: false,
      depth: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
      stencil: false,
    });
    if (gl === null) throw new Error("WebGL2 is required for editor previews");
    if (gl.getExtension("EXT_color_buffer_float") === null) {
      throw new Error("WebGL2 EXT_color_buffer_float is required for editor pixel sampling");
    }
    this._canvas = canvas;
    this._gl = gl;
    this._program = createProgram(gl);
    this._uniforms = uniformLocations(gl, this._program);
    this._vao = gl.createVertexArray();
    this._sampleTexture = gl.createTexture();
    this._sampleFramebuffer = gl.createFramebuffer();
    if (this._vao === null || this._sampleTexture === null || this._sampleFramebuffer === null) {
      throw new Error("WebGL2 could not allocate editor preview resources");
    }
    this._sources = new Map();
    this._activeSource = null;
    this._recipe = validateRecipe(DEFAULT_RECIPE);
    this._sampleWidth = 0;
    this._sampleHeight = 0;
    this._frame = null;
    this._destroyed = false;
    this._contextLost = false;
    this._onContextLost = () => {
      this._contextLost = true;
      if (this._frame !== null) cancelAnimationFrame(this._frame);
      this._frame = null;
    };
    this._canvas.addEventListener("webglcontextlost", this._onContextLost);
    gl.disable(gl.BLEND);
    gl.disable(gl.CULL_FACE);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.SCISSOR_TEST);
    gl.bindVertexArray(this._vao);
    gl.useProgram(this._program);
    gl.uniform1i(this._uniforms.u_image, 0);
    this._assertNoGlError("initialization");
  }

  get source() {
    this._assertUsable();
    return this._activeSource;
  }

  get dimensions() {
    this._assertUsable();
    if (this._activeSource === null) return null;
    const source = this._sources.get(this._activeSource);
    return Object.freeze({ width: source.width, height: source.height });
  }

  async loadSource(sourceName, url) {
    this._assertUsable();
    validateSourceName(sourceName);
    if (typeof url !== "string" || url.length === 0) throw new TypeError("Editor preview URL is required");
    const resolved = new URL(url, globalThis.location.href);
    if (resolved.origin !== globalThis.location.origin) {
      throw new DOMException("Editor preview images must use same-origin URLs", "SecurityError");
    }
    const cached = this._sources.get(sourceName);
    if (cached !== undefined && cached.url === resolved.href) {
      return Object.freeze({ width: cached.width, height: cached.height });
    }
    const response = await fetch(resolved.href, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Editor preview image request failed with HTTP ${response.status}`);
    const contentType = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
    if (contentType !== "image/jpeg") throw new Error(`Editor preview source must be image/jpeg, received ${contentType || "no type"}`);
    if (typeof createImageBitmap !== "function") throw new Error("This browser cannot decode editor preview images");
    const bitmap = await createImageBitmap(await response.blob(), {
      colorSpaceConversion: "none",
      imageOrientation: "flipY",
      premultiplyAlpha: "none",
    });
    if (bitmap.width <= 0 || bitmap.height <= 0) {
      bitmap.close();
      throw new Error("Editor preview image has invalid dimensions");
    }
    const gl = this._gl;
    const maximumSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    if (bitmap.width > maximumSize || bitmap.height > maximumSize) {
      bitmap.close();
      throw new RangeError(`Editor preview image exceeds the WebGL2 texture limit of ${maximumSize}px`);
    }
    const texture = gl.createTexture();
    if (texture === null) {
      bitmap.close();
      throw new Error("WebGL2 could not allocate an editor preview texture");
    }
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, bitmap);
    const width = bitmap.width;
    const height = bitmap.height;
    bitmap.close();
    const uploadError = gl.getError();
    if (uploadError !== gl.NO_ERROR) {
      gl.deleteTexture(texture);
      throw new Error(`Editor preview texture upload failed: ${glErrorName(gl, uploadError)}`);
    }
    if (cached !== undefined) gl.deleteTexture(cached.texture);
    const loaded = Object.freeze({ url: resolved.href, width, height, texture });
    this._sources.set(sourceName, loaded);
    if (this._activeSource === sourceName) this.switchSource(sourceName);
    return Object.freeze({ width: loaded.width, height: loaded.height });
  }

  switchSource(sourceName) {
    this._assertUsable();
    validateSourceName(sourceName);
    const source = this._sources.get(sourceName);
    if (source === undefined) throw new Error(`Editor preview source ${sourceName} is not loaded`);
    this._activeSource = sourceName;
    this._canvas.width = source.width;
    this._canvas.height = source.height;
    this._scheduleRender();
  }

  setRecipe(recipe) {
    this._assertUsable();
    this._recipe = validateRecipe(recipe);
    this._scheduleRender();
  }

  samplePixels(rectangle, stage = "source") {
    this._assertUsable();
    if (this._activeSource === null) throw new Error("Load and select an editor preview source before sampling");
    if (!["source", "rendered"].includes(stage)) throw new TypeError("Editor preview sample stage must be source or rendered");
    const source = this._sources.get(this._activeSource);
    const rectangleFields = ["x", "y", "width", "height"];
    if (rectangle === null || typeof rectangle !== "object" || rectangleFields.some((name) => !Number.isInteger(rectangle[name]))) {
      throw new TypeError("Editor preview sample rectangle must contain integer x, y, width, and height");
    }
    const { x, y, width, height } = rectangle;
    if (x < 0 || y < 0 || width <= 0 || height <= 0 || x + width > source.width || y + height > source.height) {
      throw new RangeError("Editor preview sample rectangle must stay inside the selected source");
    }
    this._prepareSampleTarget(width, height);
    this._draw(
      this._sampleFramebuffer,
      width,
      height,
      stage === "source" ? 1 : 0,
      [x / source.width, 1 - (y + height) / source.height],
      [width / source.width, height / source.height],
    );
    const gl = this._gl;
    const bottomUp = new Float32Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.FLOAT, bottomUp);
    this._assertNoGlError("pixel sampling");
    const pixels = new Float32Array(bottomUp.length);
    const rowLength = width * 4;
    for (let row = 0; row < height; row += 1) {
      const sourceStart = (height - row - 1) * rowLength;
      pixels.set(bottomUp.subarray(sourceStart, sourceStart + rowLength), row * rowLength);
    }
    return Object.freeze({
      width,
      height,
      colorSpace: stage === "source" ? "linear-rec2020" : "srgb",
      pixels,
    });
  }

  destroy() {
    if (this._destroyed) return;
    if (this._frame !== null) cancelAnimationFrame(this._frame);
    this._canvas.removeEventListener("webglcontextlost", this._onContextLost);
    if (!this._contextLost && !this._gl.isContextLost()) {
      this._sources.forEach((source) => this._gl.deleteTexture(source.texture));
      this._gl.deleteTexture(this._sampleTexture);
      this._gl.deleteFramebuffer(this._sampleFramebuffer);
      this._gl.deleteVertexArray(this._vao);
      this._gl.deleteProgram(this._program);
    }
    this._sources.clear();
    this._activeSource = null;
    this._frame = null;
    this._destroyed = true;
  }

  _scheduleRender() {
    if (this._activeSource === null || this._frame !== null) return;
    this._frame = requestAnimationFrame(() => {
      this._frame = null;
      this._assertUsable();
      this._draw(null, this._canvas.width, this._canvas.height, 0, [0, 0], [1, 1]);
    });
  }

  _prepareSampleTarget(width, height) {
    if (width === this._sampleWidth && height === this._sampleHeight) return;
    const gl = this._gl;
    gl.bindTexture(gl.TEXTURE_2D, this._sampleTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, width, height, 0, gl.RGBA, gl.FLOAT, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._sampleFramebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this._sampleTexture, 0);
    const status = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
    if (status !== gl.FRAMEBUFFER_COMPLETE) {
      throw new Error(`Editor preview sample framebuffer is incomplete: 0x${status.toString(16)}`);
    }
    this._sampleWidth = width;
    this._sampleHeight = height;
    this._assertNoGlError("sample framebuffer allocation");
  }

  _draw(framebuffer, width, height, outputMode, uvOrigin, uvScale) {
    this._assertUsable();
    const source = this._sources.get(this._activeSource);
    const recipe = this._recipe;
    const gl = this._gl;
    const uniforms = this._uniforms;
    gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
    gl.viewport(0, 0, width, height);
    gl.bindVertexArray(this._vao);
    gl.useProgram(this._program);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, source.texture);
    gl.uniform2fv(uniforms.u_uv_origin, uvOrigin);
    gl.uniform2fv(uniforms.u_uv_scale, uvScale);
    gl.uniform1i(uniforms.u_output_mode, outputMode);
    gl.uniform1i(uniforms.u_material, { positive: 0, color_negative: 1, bw_negative: 2 }[recipe.material]);
    gl.uniform1f(uniforms.u_exposure_ev, recipe.exposure_ev);
    gl.uniform1f(uniforms.u_temperature, recipe.temperature);
    gl.uniform1f(uniforms.u_tint, recipe.tint);
    gl.uniform1f(uniforms.u_contrast, recipe.contrast);
    gl.uniform1f(uniforms.u_highlights, recipe.highlights);
    gl.uniform1f(uniforms.u_shadows, recipe.shadows);
    gl.uniform1f(uniforms.u_black_point, recipe.black_point);
    gl.uniform1f(uniforms.u_white_point, recipe.white_point);
    gl.uniform1f(uniforms.u_saturation, recipe.saturation);
    gl.uniform3f(uniforms.u_rgb_balance, recipe.red_balance, recipe.green_balance, recipe.blue_balance);
    gl.uniform3f(uniforms.u_film_base, recipe.film_base_red, recipe.film_base_green, recipe.film_base_blue);
    gl.uniform1f(uniforms.u_film_density, recipe.film_density);
    gl.uniform1f(uniforms.u_film_dmin, recipe.film_dmin);
    gl.uniform1f(uniforms.u_film_dmax, recipe.film_dmax);
    gl.uniform1f(uniforms.u_film_red_ratio, recipe.film_red_ratio);
    gl.uniform1f(uniforms.u_film_blue_ratio, recipe.film_blue_ratio);
    gl.uniform1f(uniforms.u_slide_fade, recipe.slide_fade);
    gl.uniform3f(uniforms.u_slide_black, recipe.slide_black_red, recipe.slide_black_green, recipe.slide_black_blue);
    gl.uniform3f(uniforms.u_slide_white, recipe.slide_white_red, recipe.slide_white_green, recipe.slide_white_blue);
    gl.uniform1i(uniforms.u_tone_active, recipe.tone_curve.some((value, index) => value !== IDENTITY_TONE_CURVE[index]) ? 1 : 0);
    gl.uniform1fv(uniforms["u_tone_curve[0]"], recipe.tone_curve);
    const hslActive = [recipe.hsl_hue, recipe.hsl_saturation, recipe.hsl_lightness].some((values) => values.some((value) => value !== 0));
    gl.uniform1i(uniforms.u_hsl_active, hslActive ? 1 : 0);
    gl.uniform1fv(uniforms["u_hsl_hue[0]"], recipe.hsl_hue);
    gl.uniform1fv(uniforms["u_hsl_saturation[0]"], recipe.hsl_saturation);
    gl.uniform1fv(uniforms["u_hsl_lightness[0]"], recipe.hsl_lightness);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this._assertNoGlError("rendering");
  }

  _assertNoGlError(operation) {
    const error = this._gl.getError();
    if (error !== this._gl.NO_ERROR) {
      throw new Error(`Editor preview WebGL2 ${operation} failed: ${glErrorName(this._gl, error)}`);
    }
  }

  _assertUsable() {
    if (this._destroyed) throw new Error("EditorPreviewRenderer has been destroyed");
    if (this._contextLost || this._gl.isContextLost()) throw new Error("Editor preview WebGL2 context was lost");
  }
}
