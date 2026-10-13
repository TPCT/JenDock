// expect: line 2: missing required 'agent'
pipeline {
    stages { stage('A') { steps { echo 'x' } } }
}
