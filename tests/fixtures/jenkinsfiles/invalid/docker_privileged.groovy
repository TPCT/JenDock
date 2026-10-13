// expect: line 3: '--privileged' is not allowed
pipeline {
    agent { docker { image 'node:22'; args '--privileged' } }
    stages { stage('A') { steps { sh 'id' } } }
}
