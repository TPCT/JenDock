// expect: line 4: unterminated block comment
pipeline {
    agent any
    /* never closed
    stages { stage('A') { steps { echo 'x' } } }
}
