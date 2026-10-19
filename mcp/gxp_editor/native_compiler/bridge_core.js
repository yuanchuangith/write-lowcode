// Compiler protocol and structural checks, recovered from the original bridge.
// All elements, node generation, code generation and relations come from native frontend source.
import crypto from 'node:crypto';
import { defaultGenerateJsCode, defaultGenerateCsharpCode } from '@common/ActionDesign/utils';
import { ElementListData, ElementListMaps } from '@common/ActionDesign/Element';
import { globalVariablesGenerateJsCode } from '@common/ActionDesign/Variable';
import createForOfIteratorHelper from '@babel/runtime/helpers/createForOfIteratorHelper';
import slicedToArray from '@babel/runtime/helpers/slicedToArray';
import toConsumableArray from '@babel/runtime/helpers/toConsumableArray';
import objectSpread2 from '@babel/runtime/helpers/objectSpread2';
function clone(value) {
  return JSON.parse(JSON.stringify(value));
}
function fail(message) {
  throw new Error(message);
}
function sha256(value) {
  var text = typeof value === 'string' ? value : JSON.stringify(value);
  return crypto.createHash('sha256').update(text).digest('hex');
}
function jsonSafe(value) {
  if (value === undefined || typeof value === 'function' || typeof value === 'symbol') return undefined;
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(jsonSafe).filter(function (item) {
    return item !== undefined;
  });
  var result = {};
  Object.entries(value).forEach(function (_ref) {
    var _ref2 = slicedToArray(_ref, 2),
      key = _ref2[0],
      item = _ref2[1];
    if (key === 'componentType' || key === 'image') return;
    var safe = jsonSafe(item);
    if (safe !== undefined) result[key] = safe;
  });
  return result;
}
function registryElements() {
  return ElementListData.filter(Boolean).map(function (element) {
    return {
      key: element.key,
      name: element.name,
      group: element.group,
      description: element.description,
      hidden: !!element.hide,
      level_marker: !!element.levelMarker,
      end_marker: !!element.endMarker,
      end_element_key: element.endElementKey || null,
      supports_js: typeof element.generateJsCode === 'function',
      supports_csharp: typeof element.generateCsharpCode === 'function',
      input_params: jsonSafe(element.inputParams || []),
      output_params: jsonSafe(element.outputParams || []),
      supported: typeof element.generateActionData === 'function'
    };
  });
}
function describeRegistry() {
  var elements = registryElements();
  return {
    command: 'describe_registry',
    frontend_version: __GXP_FRONTEND_VERSION__,
    rule_source_sha256: __GXP_RULE_SOURCE_SHA256__,
    rule_source_file_count: __GXP_RULE_SOURCE_FILE_COUNT__,
    registry_hash: sha256({
      rule_source_sha256: __GXP_RULE_SOURCE_SHA256__,
      elements
    }),
    elements
  };
}
function actionsOf(design) {
  if (!design || typeof design !== 'object' || !Array.isArray(design.actionData)) {
    fail('design.actionData must be an array');
  }
  return design.actionData;
}
function elementFor(key) {
  var element = ElementListMaps[key];
  if (!element || typeof element.generateActionData !== 'function') {
    fail("Unknown or unsupported elementKey: ".concat(key));
  }
  return element;
}
function sameStringArray(left, right) {
  return Array.isArray(left) && left.length === right.length && left.every(function (item, index) {
    return item === right[index];
  });
}
function referencedValues(value, visit) {
  if (!value || typeof value !== 'object') return;
  if (Array.isArray(value)) {
    value.forEach(function (item) {
      return referencedValues(item, visit);
    });
    return;
  }
  var record = value;
  if (typeof record.paramTypes === 'string') visit(record);
  Object.values(record).forEach(function (item) {
    return referencedValues(item, visit);
  });
}
function validateReferences(design, action, node, actionKeys, warnings) {
  var globalNames = new Set((design.globalVariables || []).map(function (item) {
    return String(item.name || '');
  }));
  var inputNames = new Set((action.inputParams || []).flatMap(function (item) {
    return [String(item.key || ''), String(item.title || '')];
  }));
  var outputNames = new Set((action.outputParams || []).flatMap(function (item) {
    return [String(item.key || ''), String(item.title || '')];
  }));
  referencedValues(node.paramsValue, function (item) {
    var type = item.paramTypes;
    var value = String(item.value || '');
    if (type === 'globalVariable') {
      var name = value.startsWith('globalVariable-') ? value.slice('globalVariable-'.length) : String(item.label || '');
      if (name && !globalNames.has(name)) warnings.push("Node ".concat(node.key, " references missing global variable ").concat(name));
    }
    if (type === 'inputParam') {
      var _name = value.replace(/^inputParam-/, '');
      if (_name && !inputNames.has(_name)) warnings.push("Node ".concat(node.key, " references missing action input ").concat(_name));
    }
    if (type === 'outputParam') {
      var _name2 = value.replace(/^outputParam-/, '');
      if (_name2 && _name2 !== 'outputParamObject' && !outputNames.has(_name2)) {
        warnings.push("Node ".concat(node.key, " references missing action output ").concat(_name2));
      }
    }
  });
  if (node.elementKey === 'CallAction') {
    var _node$paramsValue, _node$paramsValue$inp, _node$paramsValue$inp2;
    var target = String(((_node$paramsValue = node.paramsValue) === null || _node$paramsValue === void 0 ? void 0 : (_node$paramsValue$inp = _node$paramsValue.inputParams) === null || _node$paramsValue$inp === void 0 ? void 0 : (_node$paramsValue$inp2 = _node$paramsValue$inp.actionName) === null || _node$paramsValue$inp2 === void 0 ? void 0 : _node$paramsValue$inp2.value) || '');
    if (!target || !actionKeys.has(target)) warnings.push("Node ".concat(node.key, " references missing child action ").concat(target || '<empty>'));
    var targetAction = actionsOf(design).find(function (item) {
      return item.key === target;
    });
    if (action.actionType === 'csharp' && (targetAction === null || targetAction === void 0 ? void 0 : targetAction.actionType) !== 'csharp') {
      warnings.push("C# action ".concat(action.key, " cannot call non-C# child action ").concat(target));
    }
  }
}
function validateDesign(designInput) {
  var design = clone(designInput);
  var actions = actionsOf(design);
  var actionKeys = new Set();
  var nodeKeys = new Set();
  var nodeCount = 0;
  var warnings = [];
  var _iterator = createForOfIteratorHelper(actions),
    _step;
  try {
    for (_iterator.s(); !(_step = _iterator.n()).done;) {
      var action = _step.value;
      if (!action || typeof action !== 'object' || !action.key) fail('Every action group requires a key');
      if (actionKeys.has(action.key)) fail("Duplicate action group key: ".concat(action.key));
      actionKeys.add(action.key);
      if (action.actionType !== 'js' && action.actionType !== 'csharp') {
        fail("Action ".concat(action.key, " has unsupported actionType ").concat(String(action.actionType)));
      }
      if (!Array.isArray(action.data)) fail("Action ".concat(action.key, ".data must be an array"));
    }
  } catch (err) {
    _iterator.e(err);
  } finally {
    _iterator.f();
  }
  var _iterator2 = createForOfIteratorHelper(actions),
    _step2;
  try {
    for (_iterator2.s(); !(_step2 = _iterator2.n()).done;) {
      var _action = _step2.value;
      var stack = [];
      for (var index = 0; index < _action.data.length; index += 1) {
        var node = _action.data[index];
        if (!(node === null || node === void 0 ? void 0 : node.key)) fail("Action ".concat(_action.key, " node ").concat(index, " has no key"));
        if (nodeKeys.has(node.key)) fail("Duplicate node key: ".concat(node.key));
        nodeKeys.add(node.key);
        var element = elementFor(node.elementKey);
        if (_action.actionType === 'js' && typeof element.generateJsCode !== 'function') {
          fail("Element ".concat(node.elementKey, " is not valid in JS action ").concat(_action.key));
        }
        if (_action.actionType === 'csharp' && typeof element.generateCsharpCode !== 'function') {
          fail("Element ".concat(node.elementKey, " is not valid in C# action ").concat(_action.key));
        }
        if (!node.paramsValue || typeof node.paramsValue !== 'object') {
          fail("Node ".concat(node.key, " has no paramsValue"));
        }
        if (!sameStringArray(node.depth || [], stack)) {
          fail("Node ".concat(node.key, " has invalid depth; expected [").concat(stack.join(','), "]"));
        }
        if (element.endMarker) {
          if (stack.length === 0) fail("Node ".concat(node.key, " closes a block but no block is open"));
          stack.pop();
        }
        if (element.levelMarker) stack.push(node.key);
        validateReferences(design, _action, node, actionKeys, warnings);
        nodeCount += 1;
      }
      if (stack.length > 0) fail("Action ".concat(_action.key, " contains ").concat(stack.length, " unclosed block(s)"));
    }
  } catch (err) {
    _iterator2.e(err);
  } finally {
    _iterator2.f();
  }
  return {
    validation: {
      valid: true,
      warnings,
      action_count: actions.length,
      node_count: nodeCount,
      registry_hash: describeRegistry().registry_hash,
      frontend_version: __GXP_FRONTEND_VERSION__,
      rule_source_sha256: __GXP_RULE_SOURCE_SHA256__
    }
  };
}
function actionByKey(design, key) {
  var action = actionsOf(design).find(function (item) {
    return item.key === key;
  });
  if (!action) fail("Action group ".concat(key, " was not found"));
  return action;
}
function nodeIndex(action, key) {
  var index = action.data.findIndex(function (item) {
    return item.key === key;
  });
  if (index < 0) fail("Node ".concat(key, " was not found in action group ").concat(action.key));
  return index;
}
function blockRange(action, startIndex) {
  var start = action.data[startIndex];
  var startElement = elementFor(start.elementKey);
  if (!startElement.levelMarker) return [startIndex, startIndex];
  var depth = 1;
  for (var index = startIndex + 1; index < action.data.length; index += 1) {
    var element = elementFor(action.data[index].elementKey);
    if (element.endMarker) depth -= 1;
    if (element.levelMarker) depth += 1;
    if (depth === 0) return [startIndex, index];
  }
  fail("Block beginning at node ".concat(start.key, " is not closed"));
}
function generateNodes(action, elementKey, inputParams, outputParams) {
  var element = elementFor(elementKey);
  if (action.actionType === 'js' && typeof element.generateJsCode !== 'function') {
    fail("Element ".concat(elementKey, " cannot be inserted into a JS action"));
  }
  if (action.actionType === 'csharp' && typeof element.generateCsharpCode !== 'function') {
    fail("Element ".concat(elementKey, " cannot be inserted into a C# action"));
  }
  var generated = element.generateActionData.call(element, clone(inputParams || {}), clone(outputParams || {}));
  if (!Array.isArray(generated) || generated.length === 0) fail("Element ".concat(elementKey, " generated no canvas nodes"));
  var _iterator3 = createForOfIteratorHelper(generated),
    _step3;
  try {
    for (_iterator3.s(); !(_step3 = _iterator3.n()).done;) {
      var node = _step3.value;
      if (!(node === null || node === void 0 ? void 0 : node.key) || !node.elementKey || !node.paramsValue) fail("Element ".concat(elementKey, " generated an invalid node"));
    }
  } catch (err) {
    _iterator3.e(err);
  } finally {
    _iterator3.f();
  }
  return clone(generated);
}
function recomputeDepths(design) {
  var _iterator4 = createForOfIteratorHelper(actionsOf(design)),
    _step4;
  try {
    for (_iterator4.s(); !(_step4 = _iterator4.n()).done;) {
      var action = _step4.value;
      var depth = [];
      var _iterator5 = createForOfIteratorHelper(action.data),
        _step5;
      try {
        for (_iterator5.s(); !(_step5 = _iterator5.n()).done;) {
          var node = _step5.value;
          var element = elementFor(node.elementKey);
          node.depth = [].concat(depth);
          if (element.endMarker) {
            if (depth.length === 0) fail("Node ".concat(node.key, " closes a block but no block is open"));
            depth.pop();
          }
          if (element.levelMarker) depth.push(node.key);
        }
      } catch (err) {
        _iterator5.e(err);
      } finally {
        _iterator5.f();
      }
      if (depth.length > 0) fail("Action ".concat(action.key, " contains ").concat(depth.length, " unclosed block(s)"));
    }
  } catch (err) {
    _iterator4.e(err);
  } finally {
    _iterator4.f();
  }
}
function applyOperations(design, operations) {
  var diff = [];
  var allowed = new Set(['insert_before', 'insert_after', 'replace_params', 'replace_block', 'delete_block', 'move_block']);
  var _loop = function _loop() {
    var operation = operations[operationIndex] || {};
    if (!allowed.has(operation.operation)) fail("Unsupported canvas operation: ".concat(String(operation.operation)));
    for (var _i = 0, _arr = ['key', 'depth', 'title', 'description', 'js_code', 'csharp_code', 'relation_data', 'sql', 'raw_code']; _i < _arr.length; _i++) {
      var forbidden = _arr[_i];
      if (Object.prototype.hasOwnProperty.call(operation, forbidden)) fail("Operation contains forbidden field ".concat(forbidden));
    }
    var action = actionByKey(design, String(operation.action_group_key || ''));
    var beforeKeys = action.data.map(function (item) {
      return item.key;
    });
    var generated = [];
    var affected = [];
    if (operation.operation === 'insert_before' || operation.operation === 'insert_after') {
      var _action$data;
      var anchor = nodeIndex(action, String(operation.anchor_node_key || ''));
      generated = generateNodes(action, String(operation.element_key || ''), operation.input_params, operation.output_params);
      (_action$data = action.data).splice.apply(_action$data, [anchor + (operation.operation === 'insert_after' ? 1 : 0), 0].concat(toConsumableArray(generated)));
      affected = generated.map(function (item) {
        return item.key;
      });
    } else if (operation.operation === 'replace_params') {
      var target = nodeIndex(action, String(operation.target_node_key || ''));
      var current = action.data[target];
      var replacements = generateNodes(action, current.elementKey, operation.input_params, operation.output_params);
      var replacement = replacements[0];
      action.data[target] = objectSpread2(objectSpread2({}, replacement), {}, {
        key: current.key,
        depth: current.depth
      });
      affected = [current.key];
    } else if (operation.operation === 'replace_block' || operation.operation === 'delete_block') {
      var _target = nodeIndex(action, String(operation.target_node_key || ''));
      var _blockRange = blockRange(action, _target),
        _blockRange2 = slicedToArray(_blockRange, 2),
        start = _blockRange2[0],
        end = _blockRange2[1];
      affected = action.data.slice(start, end + 1).map(function (item) {
        return item.key;
      });
      if (operation.operation === 'replace_block') {
        var _action$data2;
        generated = generateNodes(action, String(operation.element_key || ''), operation.input_params, operation.output_params);
        (_action$data2 = action.data).splice.apply(_action$data2, [start, end - start + 1].concat(toConsumableArray(generated)));
      } else {
        action.data.splice(start, end - start + 1);
      }
    } else if (operation.operation === 'move_block') {
      var _action$data3;
      var _target2 = nodeIndex(action, String(operation.target_node_key || ''));
      var _blockRange3 = blockRange(action, _target2),
        _blockRange4 = slicedToArray(_blockRange3, 2),
        _start = _blockRange4[0],
        _end = _blockRange4[1];
      var anchorKey = String(operation.anchor_node_key || '');
      if (action.data.slice(_start, _end + 1).some(function (item) {
        return item.key === anchorKey;
      })) fail('Move anchor is inside the moved block');
      var moved = action.data.splice(_start, _end - _start + 1);
      var _anchor = nodeIndex(action, anchorKey);
      var position = operation.position;
      if (position !== 'before' && position !== 'after') fail('move_block position must be before or after');
      (_action$data3 = action.data).splice.apply(_action$data3, [_anchor + (position === 'after' ? 1 : 0), 0].concat(toConsumableArray(moved)));
      affected = moved.map(function (item) {
        return item.key;
      });
    }
    diff.push({
      operation_index: operationIndex,
      operation: operation.operation,
      action_group_key: action.key,
      anchor_node_key: operation.anchor_node_key || null,
      target_node_key: operation.target_node_key || null,
      element_key: operation.element_key || null,
      affected_node_keys: affected,
      generated_nodes: generated.map(function (item) {
        return {
          key: item.key,
          element_key: item.elementKey
        };
      }),
      before_node_count: beforeKeys.length,
      after_node_count: action.data.length
    });
  };
  for (var operationIndex = 0; operationIndex < operations.length; operationIndex += 1) {
    _loop();
  }
  return diff;
}
function compileCode(design) {
  var relationData = {};
  var jsActions = design.actionData.filter(function (item) {
    return item.actionType === 'js';
  });
  var jsCode = '';
  if (jsActions.length > 0) {
    jsCode = 'function (inbiz,relatedAttributes) {';
    jsCode += '\n\tvar self = {};';
    globalVariablesGenerateJsCode(design.globalVariables || []).split('\n').forEach(function (line) {
      jsCode += "\n\t".concat(line);
    });
    jsActions.forEach(function (action) {
      var _action$data4;
      (_action$data4 = action.data) === null || _action$data4 === void 0 ? void 0 : _action$data4.forEach(function (node) {
        var element = elementFor(node.elementKey);
        if (element.generateRelationData) {
          var _node$paramsValue2, _node$paramsValue3;
          relationData[node.key] = element.generateRelationData((_node$paramsValue2 = node.paramsValue) === null || _node$paramsValue2 === void 0 ? void 0 : _node$paramsValue2.inputParams, (_node$paramsValue3 = node.paramsValue) === null || _node$paramsValue3 === void 0 ? void 0 : _node$paramsValue3.outputParams, node, design);
        }
      });
      jsCode += defaultGenerateJsCode(action, design);
    });
    jsCode += '\n\treturn self;\n';
    jsCode += '\n}';
  }
  var csharpActions = design.actionData.filter(function (item) {
    return item.actionType === 'csharp';
  });
  var csharpCode = '';
  if (csharpActions.length > 0) {
    if (!design.code || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(design.code)) {
      fail('design.code must be a valid C# identifier when C# actions exist');
    }
    csharpCode = "using System;\nusing System.Collections;\nusing System.Collections.Generic;\nusing System.IO;\nusing System.Linq;\nusing System.Text;\nusing System.Threading.Tasks;\nusing Microsoft.AspNetCore.Http;\nusing Microsoft.Extensions.DependencyInjection;\nusing Newtonsoft.Json;\nusing System.Reflection;\nusing GxP2.Services;\nusing FreeSql;\nusing FreeSql.Internal.Model;\nusing GxP2;\nusing GxP2.Data;\nusing GxP2.Model;\nusing GxP2.Model.Dto;\nusing Newtonsoft.Json.Linq;\n\nnamespace ".concat(design.code, "\n{\n    public class ").concat(design.code, " : BizflowsCsharpServiceBase\n    {\n        public ").concat(design.code, "(IFreeSql dataContext, IHttpContextAccessor httpContextAccessor, BizflowsCsharpDto dto) : base(dataContext, httpContextAccessor, dto)\n        {}\n");
    csharpActions.forEach(function (action) {
      var _action$data5;
      (_action$data5 = action.data) === null || _action$data5 === void 0 ? void 0 : _action$data5.forEach(function (node) {
        var element = elementFor(node.elementKey);
        if (element.generateRelationData) {
          var _node$paramsValue4, _node$paramsValue5;
          relationData[node.key] = element.generateRelationData((_node$paramsValue4 = node.paramsValue) === null || _node$paramsValue4 === void 0 ? void 0 : _node$paramsValue4.inputParams, (_node$paramsValue5 = node.paramsValue) === null || _node$paramsValue5 === void 0 ? void 0 : _node$paramsValue5.outputParams, node, design);
        }
      });
      csharpCode += defaultGenerateCsharpCode(action, design);
    });
    csharpCode += '\t}\n}';
  }
  return {
    js_code: jsCode,
    csharp_code: csharpCode,
    relation_data: relationData
  };
}
function compilePatch(designInput, operations) {
  if (!Array.isArray(operations) || operations.length === 0) fail('At least one canvas operation is required');
  validateDesign(designInput);
  var design = clone(designInput);
  var diff = applyOperations(design, operations);
  recomputeDepths(design);
  var validation = validateDesign(design).validation;
  var compiled = compileCode(design);
  return objectSpread2({
    command: 'compile_patch',
    design,
    diff,
    validation
  }, compiled);
}
function executeCompilerCommand(payload) {
  switch (payload === null || payload === void 0 ? void 0 : payload.command) {
    case 'describe_registry':
      return describeRegistry();
    case 'validate_design':
      return objectSpread2({
        command: 'validate_design'
      }, validateDesign(payload.design));
    case 'compile_patch':
      return compilePatch(payload.design, payload.operations);
    default:
      fail("Unsupported compiler command: ".concat(String((payload === null || payload === void 0 ? void 0 : payload.command) || '')));
  }
}

export { executeCompilerCommand };
