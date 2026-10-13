// expect: line 4: must match [A-Z_][A-Z0-9_]*
pipeline {
    agent any
    environment { lower_case = 'x' }
    stages { stage('A') { steps { echo 'x' } } }
}
