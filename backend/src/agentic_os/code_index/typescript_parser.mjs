import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const typescriptUrl = pathToFileURL(
  path.join(
    request.repository,
    "frontend/node_modules/typescript/lib/typescript.js",
  ),
);
let ts;
try {
  ts = await import(typescriptUrl.href);
} catch (error) {
  process.stderr.write(
    `TypeScript compiler API is unavailable: ${error.message}\n`,
  );
  process.exit(2);
}

const source = ts.createSourceFile(
  request.path,
  Buffer.from(request.content, "base64").toString("utf8"),
  ts.ScriptTarget.Latest,
  true,
  request.path.endsWith(".tsx")
    ? ts.ScriptKind.TSX
    : request.path.endsWith(".jsx")
      ? ts.ScriptKind.JSX
      : request.path.endsWith(".js") || request.path.endsWith(".mjs")
        ? ts.ScriptKind.JS
        : ts.ScriptKind.TS,
);

if (source.parseDiagnostics.length) {
  const diagnostic = source.parseDiagnostics[0];
  const location = source.getLineAndCharacterOfPosition(diagnostic.start ?? 0);
  process.stderr.write(
    `${request.path}:${location.line + 1}:${location.character + 1}: ${ts.flattenDiagnosticMessageText(diagnostic.messageText, " ")}\n`,
  );
  process.exit(2);
}

const moduleName = request.path
  .replace(/^frontend\//, "")
  .replace(/\.(tsx?|mts|mjs|jsx?)$/, "")
  .replace(/\/index$/, "")
  .replaceAll("/", ".");

function location(node) {
  const start = source.getLineAndCharacterOfPosition(node.getStart(source));
  const end = source.getLineAndCharacterOfPosition(
    Math.max(node.getStart(source), node.getEnd() - 1),
  );
  return {
    startLine: start.line + 1,
    startColumn: start.character + 1,
    endLine: end.line + 1,
    endColumn: end.character + 1,
  };
}

function modifiers(node) {
  return (node.modifiers ?? [])
    .map((item) => ts.tokenToString(item.kind))
    .filter(Boolean);
}

function visibility(node, name) {
  const values = modifiers(node);
  if (values.includes("private") || name.startsWith("_")) return "private";
  if (values.includes("protected")) return "protected";
  return "public";
}

function expressionName(node) {
  if (ts.isIdentifier(node) || node.kind === ts.SyntaxKind.ThisKeyword)
    return node.getText(source);
  if (ts.isPropertyAccessExpression(node)) {
    const parent = expressionName(node.expression);
    return parent ? `${parent}.${node.name.text}` : null;
  }
  return null;
}

function signature(node) {
  const typeParameters =
    node.typeParameters?.map((item) => item.getText(source)).join(",") ?? "";
  const parameters =
    node.parameters?.map((item) => item.getText(source)).join(", ") ?? "";
  const result = node.type ? `: ${node.type.getText(source)}` : "";
  return `${typeParameters ? `<${typeParameters}>` : ""}(${parameters})${result}`;
}

const symbols = [];
const dependencies = [];

function symbol(kind, qualifiedName, node, name, extra = {}) {
  symbols.push({
    kind,
    qualifiedName,
    location: location(node),
    signature:
      kind === "function" || kind === "method" ? signature(node) : undefined,
    visibility: visibility(node, name),
    extensions: { modifiers: modifiers(node), ...extra },
  });
}

function dependency(kind, owner, target, node, resolution, extension = {}) {
  dependencies.push({
    kind,
    owner,
    target,
    location: location(node),
    resolution,
    extension,
  });
}

function visitCalls(node, owner, ownerQualifiedName, classQualifiedName) {
  function walk(current) {
    if (
      current !== node &&
      (ts.isFunctionLike(current) || ts.isClassLike(current))
    )
      return;
    if (ts.isCallExpression(current)) {
      const target = expressionName(current.expression);
      if (
        target &&
        !["String", "Number", "Boolean", "Array", "Object", "Promise"].includes(
          target,
        )
      ) {
        const extension = {};
        if (
          (target === "fetch" || target.endsWith(".fetch")) &&
          current.arguments[0] &&
          ts.isStringLiteralLike(current.arguments[0])
        ) {
          let method = "GET";
          const options = current.arguments[1];
          if (options && ts.isObjectLiteralExpression(options)) {
            const property = options.properties.find(
              (item) => item.name?.getText(source) === "method",
            );
            if (
              property &&
              ts.isPropertyAssignment(property) &&
              ts.isStringLiteralLike(property.initializer)
            )
              method = property.initializer.text.toUpperCase();
          }
          extension.http = { method, path: current.arguments[0].text };
        }
        dependency(
          "call",
          owner,
          target,
          current,
          {
            module: moduleName,
            owner: ownerQualifiedName,
            class: classQualifiedName,
            syntacticTarget: target,
          },
          extension,
        );
      }
    }
    ts.forEachChild(current, walk);
  }
  walk(node);
}

function visitStatements(statements, parent, owner, classQualifiedName = null) {
  for (const node of statements) {
    if (
      ts.isImportDeclaration(node) &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      const module = node.moduleSpecifier.text;
      const clause = node.importClause;
      if (!clause)
        dependency("import", owner, module, node, {
          form: "side-effect",
          module,
          alias: "",
        });
      if (clause?.name)
        dependency("import", owner, module, node, {
          form: "default",
          module,
          name: "default",
          alias: clause.name.text,
        });
      const bindings = clause?.namedBindings;
      if (bindings && ts.isNamespaceImport(bindings))
        dependency("import", owner, module, node, {
          form: "namespace",
          module,
          alias: bindings.name.text,
        });
      if (bindings && ts.isNamedImports(bindings))
        for (const item of bindings.elements) {
          dependency(
            "import",
            owner,
            `${module}.${item.propertyName?.text ?? item.name.text}`,
            item,
            {
              form: "named",
              module,
              name: item.propertyName?.text ?? item.name.text,
              alias: item.name.text,
            },
          );
        }
      continue;
    }
    if (ts.isClassDeclaration(node) && node.name) {
      const qualifiedName = `${parent}.${node.name.text}`;
      symbol("class", qualifiedName, node, node.name.text, {
        heritage:
          node.heritageClauses?.map((item) => item.getText(source)) ?? [],
      });
      visitStatements(
        node.members,
        qualifiedName,
        qualifiedName,
        qualifiedName,
      );
      continue;
    }
    if (
      (ts.isMethodDeclaration(node) || ts.isMethodSignature(node)) &&
      node.name
    ) {
      const name = node.name.getText(source);
      const qualifiedName = `${parent}.${name}`;
      symbol("method", qualifiedName, node, name, {
        async: modifiers(node).includes("async"),
      });
      visitCalls(node, qualifiedName, qualifiedName, classQualifiedName);
      if (node.body)
        visitStatements(
          node.body.statements,
          qualifiedName,
          qualifiedName,
          classQualifiedName,
        );
      continue;
    }
    if (ts.isFunctionDeclaration(node) && node.name) {
      const qualifiedName = `${parent}.${node.name.text}`;
      symbol("function", qualifiedName, node, node.name.text, {
        async: modifiers(node).includes("async"),
      });
      visitCalls(node, qualifiedName, qualifiedName, classQualifiedName);
      if (node.body)
        visitStatements(
          node.body.statements,
          qualifiedName,
          qualifiedName,
          classQualifiedName,
        );
      continue;
    }
    if (ts.isVariableStatement(node)) {
      for (const declaration of node.declarationList.declarations)
        if (
          ts.isIdentifier(declaration.name) &&
          declaration.initializer &&
          (ts.isArrowFunction(declaration.initializer) ||
            ts.isFunctionExpression(declaration.initializer))
        ) {
          const qualifiedName = `${parent}.${declaration.name.text}`;
          symbol(
            "function",
            qualifiedName,
            declaration,
            declaration.name.text,
            {
              async: modifiers(declaration.initializer).includes("async"),
              variable: true,
            },
          );
          visitCalls(
            declaration.initializer,
            qualifiedName,
            qualifiedName,
            classQualifiedName,
          );
          if (ts.isBlock(declaration.initializer.body))
            visitStatements(
              declaration.initializer.body.statements,
              qualifiedName,
              qualifiedName,
              classQualifiedName,
            );
        }
      continue;
    }
  }
}

visitCalls(source, moduleName, moduleName, null);
visitStatements(source.statements, moduleName, moduleName);
process.stdout.write(
  JSON.stringify({
    moduleName,
    lineCount: source.getLineAndCharacterOfPosition(source.getEnd()).line + 1,
    symbols,
    dependencies,
  }),
);
