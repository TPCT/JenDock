<?php
// Trusted parser entrypoint. Candidate source is data passed to PHP-Parser only.
declare(strict_types=1);
try {
    if ($argc !== 2) { throw new RuntimeException('parser configuration'); }
    require $argv[1] . '/autoload.php';
    $source = stream_get_contents(STDIN, 1048577);
    if ($source === false || strlen($source) > 1048576) { throw new RuntimeException('source size'); }
    $parser = (new PhpParser\ParserFactory())->createForNewestSupportedVersion();
    $nodes = $parser->parse($source);
    $result = json_encode(['nodes' => $nodes], JSON_THROW_ON_ERROR);
    if (strlen($result) > 8388608) { throw new RuntimeException('AST size'); }
    echo $result;
} catch (PhpParser\Error $error) {
    echo json_encode(['error' => 'Invalid PHP syntax', 'line' => $error->getStartLine()]);
    exit(2);
} catch (Throwable $error) {
    echo json_encode(['error' => 'Trusted PHP parser unavailable or input exceeds bounds', 'line' => 1]);
    exit(2);
}
